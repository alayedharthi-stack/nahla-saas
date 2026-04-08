import { useState, useEffect, useCallback } from 'react'
import {
  ToggleLeft, ToggleRight, Settings2, CheckCircle, AlertCircle,
  Loader2, Copy, X, Puzzle, MessageCircle, Gift, Tag,
  Zap, Link2, Sparkles,
} from 'lucide-react'
import { addonsApi, type AddonItem } from '../api/addons'

// ── Backend base URL ──────────────────────────────────────────────────────────
const API_BASE = (import.meta.env.VITE_API_URL as string) || 'https://api.nahlah.ai'

// ── Tenant ID from session ────────────────────────────────────────────────────
function getTenantId(): string {
  return localStorage.getItem('nahla_tenant_id') || ''
}

// ── Nahla CDN logo (default widget logo) ─────────────────────────────────────
const NAHLA_CDN_LOGO =
  'https://cdn.salla.sa/XVEDq/b1ec4359-6895-49dc-80e7-06fd33b75df8-1000x666.66666666667-xMM28RbT68xVWoSgEtzBgpW1w4cDN7sEQAhQmLwD.jpg'

// ── Widget code generator ─────────────────────────────────────────────────────
function generateWidgetCode(s: Record<string, unknown>): string {
  const phone  = String(s.phone            || '')
  const msg    = String(s.message          || 'السلام عليكم، أبغى الاستفسار')
  const logo   = String(s.logo_url         || '') || NAHLA_CDN_LOGO
  const pos    = s.position === 'right' ? 'right' : 'left'
  const thresh = Number(s.scroll_threshold ?? 250)
  const posX   = pos === 'right' ? 'right:40px' : 'left:40px'
  const posXMo = pos === 'right' ? 'right:20px' : 'left:20px'

  return `/* ====================================================
   Nahla WhatsApp Widget — نحلة
   انسخ هذا الكود في حقل الجافاسكريبت بمتجرك
   ==================================================== */
(function(){
  var WA_NUMBER  = '${phone}';
  var WA_MESSAGE = '${msg}';
  var LOGO = '${logo}';
  var btn = document.createElement('a');
  btn.href   = 'https://wa.me/' + WA_NUMBER + '?text=' + encodeURIComponent(WA_MESSAGE);
  btn.target = '_blank'; btn.rel = 'noopener noreferrer'; btn.id = 'nahla-whatsapp';
  btn.innerHTML =
    '<img src="' + LOGO + '" class="nahla-bee" alt="نحلة">' +
    '<div class="circle"><span class="orbit o1"></span><span class="orbit o2"></span>' +
    '<span class="orbit o3"></span><span class="orbit o4"></span>' +
    '<img src="https://upload.wikimedia.org/wikipedia/commons/6/6b/WhatsApp.svg" class="icon" alt="واتساب"></div>';
  document.body.appendChild(btn);
  function checkShow(){if(window.scrollY>${thresh}||document.body.scrollHeight<=window.innerHeight+300)btn.classList.add('show');}
  window.addEventListener('scroll',checkShow,{passive:true});checkShow();
  var s=document.createElement('style');
  s.innerHTML=['#nahla-whatsapp{position:fixed;bottom:55px;${posX};z-index:9999;opacity:0;transform:scale(.8);transition:opacity .4s,transform .4s;display:flex;flex-direction:column;align-items:center;gap:6px;text-decoration:none;}',
  '#nahla-whatsapp.show{opacity:1;transform:scale(1);}',
  '.nahla-bee{width:110px;height:110px;object-fit:contain;animation:bee-float 3s ease-in-out infinite;}',
  '@keyframes bee-float{0%,100%{transform:translateY(0) rotate(-4deg);}50%{transform:translateY(-7px) rotate(4deg);}}',
  '.circle{position:relative;width:65px;height:65px;background:#25D366;border-radius:50%;display:flex;align-items:center;justify-content:center;box-shadow:0 4px 18px rgba(37,211,102,.45);}',
  '.icon{width:30px;height:30px;z-index:2;position:relative;}',
  '.orbit{position:absolute;inset:0;border-radius:50%;border:2.5px solid rgba(37,211,102,.65);animation:apple-wave 2.8s cubic-bezier(.4,0,.2,1) infinite;}',
  '.o1{animation-delay:0s;}.o2{animation-delay:.7s;}.o3{animation-delay:1.4s;}.o4{animation-delay:2.1s;}',
  '@keyframes apple-wave{0%{transform:scale(.92);opacity:.85;}30%{transform:scale(1.25);opacity:.55;}60%{transform:scale(1.65);opacity:.22;}85%{transform:scale(1.95);opacity:.05;}100%{transform:scale(2.05);opacity:0;}}',
  '@media(max-width:600px){.circle{width:58px;height:58px;}.icon{width:26px;height:26px;}.nahla-bee{width:90px;height:90px;}#nahla-whatsapp{bottom:50px;${posXMo};}}'].join('');
  document.head.appendChild(s);
})();`
}

// ── Badge chip ────────────────────────────────────────────────────────────────
function BadgeChip({ badge }: { badge: string }) {
  if (badge === 'paid')
    return <span className="px-2 py-0.5 text-[10px] font-semibold rounded-full bg-amber-100 text-amber-700 border border-amber-200">مدفوعة</span>
  if (badge === 'coming_soon')
    return <span className="px-2 py-0.5 text-[10px] font-semibold rounded-full bg-slate-100 text-slate-500 border border-slate-200">قريباً</span>
  return <span className="px-2 py-0.5 text-[10px] font-semibold rounded-full bg-emerald-50 text-emerald-600 border border-emerald-200">مجانية</span>
}

// ── Addon icon ────────────────────────────────────────────────────────────────
function AddonIcon({ addonKey }: { addonKey: string }) {
  const cls = 'w-6 h-6'
  if (addonKey === 'widget')              return <MessageCircle className={cls} />
  if (addonKey === 'discount_popup')      return <Gift          className={cls} />
  if (addonKey === 'first_order_coupon')  return <Tag           className={cls} />
  return <Puzzle className={cls} />
}

function addonColor(key: string) {
  if (key === 'widget')             return 'bg-emerald-50 text-emerald-600 border-emerald-200'
  if (key === 'discount_popup')     return 'bg-purple-50  text-purple-600  border-purple-200'
  if (key === 'first_order_coupon') return 'bg-orange-50  text-orange-600  border-orange-200'
  return 'bg-brand-50 text-brand-600 border-brand-200'
}

// ── Settings forms ─────────────────────────────────────────────────────────────

function WidgetSettingsForm({
  settings, onChange,
}: { settings: Record<string, unknown>; onChange: (k: string, v: unknown) => void }) {
  const [tagCopied,  setTagCopied]  = useState(false)
  const [codeCopied, setCodeCopied] = useState(false)

  const tenantId  = getTenantId()
  const embedUrl  = `${API_BASE}/merchant/addons/widget/${tenantId}/embed.js`
  const scriptTag = `<script src="${embedUrl}"></script>`
  const fullCode  = generateWidgetCode(settings)

  const copyTag  = () => { navigator.clipboard.writeText(scriptTag); setTagCopied(true);  setTimeout(() => setTagCopied(false),  2500) }
  const copyCode = () => { navigator.clipboard.writeText(fullCode);  setCodeCopied(true); setTimeout(() => setCodeCopied(false), 2500) }

  return (
    <div className="space-y-5">

      {/* ── Smart embed URL (primary) ── */}
      <div className="p-4 bg-emerald-50 border border-emerald-200 rounded-xl space-y-3">
        <div className="flex items-center gap-2 text-emerald-700">
          <Sparkles className="w-4 h-4 shrink-0" />
          <p className="text-sm font-semibold">الطريقة الذكية — أضفه مرة واحدة فقط</p>
        </div>
        <p className="text-xs text-emerald-600 leading-relaxed">
          أضف هذا السطر مرة واحدة في سلة، وبعدها يكفي الضغط على "تفعيل / تعطيل" من هنا دون أي تعديل في المتجر.
        </p>
        <div className="relative">
          <code dir="ltr" className="block w-full bg-white border border-emerald-200 rounded-lg px-3 py-2.5 text-xs font-mono text-slate-700 pe-20 overflow-x-auto whitespace-nowrap">
            {scriptTag}
          </code>
          <button onClick={copyTag}
            className="absolute top-1.5 left-1.5 flex items-center gap-1 px-2.5 py-1.5 rounded-md bg-emerald-600 hover:bg-emerald-700 text-white text-xs font-medium transition-colors shrink-0">
            {tagCopied ? <CheckCircle className="w-3 h-3" /> : <Copy className="w-3 h-3" />}
            {tagCopied ? 'تم' : 'نسخ'}
          </button>
        </div>
        <div className="flex items-start gap-2 text-xs text-emerald-700 bg-emerald-100 rounded-lg p-2.5">
          <Link2 className="w-3.5 h-3.5 shrink-0 mt-0.5" />
          <div>
            <span className="font-semibold">في سلة: </span>
            الإعدادات ← مظهر المتجر ← JavaScript مخصص ← الصق الكود
          </div>
        </div>
      </div>

      {/* ── Settings fields ── */}
      <div>
        <label className="label">رقم واتساب <span className="text-red-400">*</span></label>
        <input className="input" dir="ltr" placeholder="966555906901"
          value={String(settings.phone ?? '')}
          onChange={e => onChange('phone', e.target.value.replace(/\D/g, ''))} />
        <p className="text-xs text-slate-400 mt-1">الرقم الدولي بدون + ومسافات</p>
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
            {(['right', 'left'] as const).map(p => (
              <button key={p} onClick={() => onChange('position', p)}
                className={`flex-1 py-2 text-sm rounded-lg border transition-colors ${
                  settings.position === p
                    ? 'bg-brand-500 border-brand-500 text-white font-medium'
                    : 'border-slate-200 text-slate-600 hover:bg-slate-50'
                }`}>
                {p === 'right' ? '⬅ يمين' : 'يسار ➡'}
              </button>
            ))}
          </div>
        </div>
        <div>
          <label className="label">إظهار بعد تمرير (px)</label>
          <input type="number" min={0} max={2000} className="input" dir="ltr"
            value={Number(settings.scroll_threshold ?? 250)}
            onChange={e => onChange('scroll_threshold', Number(e.target.value))} />
          <p className="text-xs text-slate-400 mt-1">0 = فوري</p>
        </div>
      </div>

      {/* ── Manual full code (fallback) ── */}
      {!!settings.phone && (
        <details className="group">
          <summary className="cursor-pointer text-xs text-slate-500 hover:text-slate-700 select-none list-none flex items-center gap-1">
            <span className="group-open:rotate-90 transition-transform inline-block">▶</span>
            أو انسخ الكود الكامل يدوياً
          </summary>
          <div className="mt-2 relative">
            <textarea readOnly dir="ltr" rows={6}
              className="w-full font-mono text-xs bg-slate-900 text-slate-100 rounded-xl p-4 resize-none border-0 outline-none leading-relaxed"
              value={fullCode} />
            <button onClick={copyCode}
              className="absolute top-3 left-3 flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-slate-700 hover:bg-slate-600 text-white text-xs font-medium transition-colors">
              {codeCopied ? <CheckCircle className="w-3.5 h-3.5 text-emerald-400" /> : <Copy className="w-3.5 h-3.5" />}
              {codeCopied ? 'تم النسخ!' : 'نسخ'}
            </button>
          </div>
        </details>
      )}
    </div>
  )
}

function DiscountPopupForm({
  settings, onChange,
}: { settings: Record<string, unknown>; onChange: (k: string, v: unknown) => void }) {
  return (
    <div className="space-y-4">
      <div>
        <label className="label">عنوان النافذة</label>
        <input className="input" value={String(settings.title ?? '')}
          onChange={e => onChange('title', e.target.value)} />
      </div>
      <div>
        <label className="label">نص العرض</label>
        <textarea className="input min-h-[80px] resize-none" rows={3}
          value={String(settings.body_text ?? '')}
          onChange={e => onChange('body_text', e.target.value)} />
      </div>
      <div className="grid grid-cols-2 gap-4">
        <div>
          <label className="label">نوع الخصم</label>
          <select className="input" value={String(settings.discount_type ?? 'percentage')}
            onChange={e => onChange('discount_type', e.target.value)}>
            <option value="percentage">نسبة مئوية %</option>
            <option value="fixed">مبلغ ثابت</option>
          </select>
        </div>
        <div>
          <label className="label">قيمة الخصم</label>
          <input type="number" min={0} className="input" dir="ltr"
            value={Number(settings.discount_value ?? 10)}
            onChange={e => onChange('discount_value', Number(e.target.value))} />
        </div>
      </div>
      <div className="grid grid-cols-2 gap-4">
        <div>
          <label className="label">وقت الظهور (ثانية)</label>
          <input type="number" min={0} className="input" dir="ltr"
            value={Number(settings.delay_seconds ?? 5)}
            onChange={e => onChange('delay_seconds', Number(e.target.value))} />
        </div>
        <div className="flex flex-col justify-end pb-1">
          <label className="flex items-center gap-2 cursor-pointer">
            <input type="checkbox" className="w-4 h-4 accent-brand-500 shrink-0"
              checked={Boolean(settings.show_once ?? true)}
              onChange={e => onChange('show_once', e.target.checked)} />
            <span className="text-sm text-slate-700">اعرضها مرة واحدة فقط</span>
          </label>
        </div>
      </div>
    </div>
  )
}

function FirstOrderCouponForm({
  settings, onChange,
}: { settings: Record<string, unknown>; onChange: (k: string, v: unknown) => void }) {
  return (
    <div className="space-y-4">
      <div>
        <label className="label">كود الكوبون <span className="text-red-400">*</span></label>
        <input className="input" dir="ltr" placeholder="WELCOME10"
          value={String(settings.coupon_code ?? '')}
          onChange={e => onChange('coupon_code', e.target.value.toUpperCase())} />
      </div>
      <div className="grid grid-cols-2 gap-4">
        <div>
          <label className="label">نوع الخصم</label>
          <select className="input" value={String(settings.discount_type ?? 'percentage')}
            onChange={e => onChange('discount_type', e.target.value)}>
            <option value="percentage">نسبة مئوية %</option>
            <option value="fixed">مبلغ ثابت</option>
          </select>
        </div>
        <div>
          <label className="label">قيمة الخصم</label>
          <input type="number" min={0} className="input" dir="ltr"
            value={Number(settings.discount_value ?? 10)}
            onChange={e => onChange('discount_value', Number(e.target.value))} />
        </div>
      </div>
      <div className="grid grid-cols-2 gap-4">
        <div>
          <label className="label">الحد الأدنى للطلب</label>
          <input type="number" min={0} className="input" dir="ltr"
            value={Number(settings.min_order_value ?? 0)}
            onChange={e => onChange('min_order_value', Number(e.target.value))} />
        </div>
        <div>
          <label className="label">صلاحية الكوبون (أيام)</label>
          <input type="number" min={1} className="input" dir="ltr"
            value={Number(settings.validity_days ?? 30)}
            onChange={e => onChange('validity_days', Number(e.target.value))} />
        </div>
      </div>
      <div>
        <label className="flex items-center gap-2 cursor-pointer">
          <input type="checkbox" className="w-4 h-4 accent-brand-500 shrink-0"
            checked={Boolean(settings.new_customers_only ?? true)}
            onChange={e => onChange('new_customers_only', e.target.checked)} />
          <span className="text-sm text-slate-700">للعملاء الجدد فقط</span>
        </label>
      </div>
    </div>
  )
}

// ── Settings Modal ────────────────────────────────────────────────────────────

interface SettingsModalProps {
  addon:    AddonItem
  onClose:  () => void
  onSaved:  (updated: AddonItem) => void
}

function SettingsModal({ addon, onClose, onSaved }: SettingsModalProps) {
  const [localSettings, setLocalSettings] = useState<Record<string, unknown>>(
    { ...addon.settings },
  )
  const [saving, setSaving] = useState(false)
  const [error,  setError]  = useState<string | null>(null)

  const handleChange = (key: string, value: unknown) =>
    setLocalSettings(prev => ({ ...prev, [key]: value }))

  const handleSave = async () => {
    setSaving(true); setError(null)
    try {
      const updated = await addonsApi.updateSettings(addon.key, localSettings)
      onSaved(updated)
      onClose()
    } catch {
      setError('فشل الحفظ — حاول مرة أخرى')
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-black/50 backdrop-blur-sm">
      <div
        className="bg-white rounded-2xl shadow-2xl w-full max-w-lg max-h-[90vh] flex flex-col"
        onClick={e => e.stopPropagation()}
      >
        {/* Header */}
        <div className="flex items-center justify-between px-6 py-4 border-b border-slate-100">
          <div className="flex items-center gap-2.5">
            <div className={`p-2 rounded-xl border ${addonColor(addon.key)}`}>
              <AddonIcon addonKey={addon.key} />
            </div>
            <div>
              <h3 className="font-semibold text-slate-800">{addon.name}</h3>
              <p className="text-xs text-slate-400">إعدادات الإضافة</p>
            </div>
          </div>
          <button onClick={onClose} className="p-1.5 rounded-lg hover:bg-slate-100 text-slate-400 transition-colors">
            <X className="w-5 h-5" />
          </button>
        </div>

        {/* Body */}
        <div className="flex-1 overflow-y-auto px-6 py-5">
          {addon.key === 'widget' && (
            <WidgetSettingsForm settings={localSettings} onChange={handleChange} />
          )}
          {addon.key === 'discount_popup' && (
            <DiscountPopupForm settings={localSettings} onChange={handleChange} />
          )}
          {addon.key === 'first_order_coupon' && (
            <FirstOrderCouponForm settings={localSettings} onChange={handleChange} />
          )}
        </div>

        {/* Footer */}
        <div className="px-6 py-4 border-t border-slate-100 flex items-center justify-between gap-3">
          {error && (
            <p className="text-sm text-red-500 flex items-center gap-1">
              <AlertCircle className="w-4 h-4" />{error}
            </p>
          )}
          <div className="flex gap-2 ms-auto">
            <button onClick={onClose}
              className="px-4 py-2 text-sm font-medium text-slate-600 hover:bg-slate-100 rounded-xl transition-colors">
              إلغاء
            </button>
            <button onClick={handleSave} disabled={saving}
              className="flex items-center gap-2 px-4 py-2 text-sm font-medium bg-brand-500 hover:bg-brand-600 text-white rounded-xl transition-colors disabled:opacity-50">
              {saving ? <Loader2 className="w-4 h-4 animate-spin" /> : <CheckCircle className="w-4 h-4" />}
              حفظ الإعدادات
            </button>
          </div>
        </div>
      </div>
    </div>
  )
}

// ── Addon Card ────────────────────────────────────────────────────────────────

interface AddonCardProps {
  addon:      AddonItem
  onToggled:  (updated: AddonItem) => void
  onSettings: (addon: AddonItem) => void
}

function AddonCard({ addon, onToggled, onSettings }: AddonCardProps) {
  const [toggling, setToggling] = useState(false)
  const [toast,    setToast]    = useState<string | null>(null)
  const isComingSoon = addon.badge === 'coming_soon'

  const handleToggle = async () => {
    if (isComingSoon || toggling) return
    setToggling(true)
    try {
      const updated = await addonsApi.toggle(addon.key, !addon.is_enabled)
      onToggled(updated)
      if (updated.is_enabled && updated.has_settings) {
        // Auto-open settings so the merchant gets the embed code immediately
        onSettings(updated)
      } else {
        setToast('تم تعطيل الإضافة')
        setTimeout(() => setToast(null), 2500)
      }
    } catch {
      setToast('فشل التعديل — حاول مرة أخرى')
      setTimeout(() => setToast(null), 3000)
    } finally {
      setToggling(false)
    }
  }

  return (
    <div className={`relative bg-white rounded-2xl border transition-all duration-200 ${
      addon.is_enabled
        ? 'border-brand-200 shadow-md shadow-brand-500/5'
        : 'border-slate-200 hover:border-slate-300 hover:shadow-sm'
    } ${isComingSoon ? 'opacity-70' : ''}`}>

      {/* Toast */}
      {toast && (
        <div className="absolute -top-10 inset-x-0 flex justify-center z-10">
          <div className="bg-slate-800 text-white text-xs px-3 py-1.5 rounded-full shadow-lg whitespace-nowrap">
            {toast}
          </div>
        </div>
      )}

      <div className="p-5">
        {/* Top row: icon + badge */}
        <div className="flex items-start justify-between mb-3">
          <div className={`p-2.5 rounded-xl border ${addonColor(addon.key)}`}>
            <AddonIcon addonKey={addon.key} />
          </div>
          <BadgeChip badge={addon.badge} />
        </div>

        {/* Name + description */}
        <h3 className="font-semibold text-slate-800 text-sm mb-1">{addon.name}</h3>
        <p className="text-xs text-slate-500 leading-relaxed mb-4">{addon.description}</p>

        {/* Status indicator */}
        <div className={`flex items-center gap-1.5 text-xs mb-4 ${
          addon.is_enabled ? 'text-emerald-600' : 'text-slate-400'
        }`}>
          <span className={`w-1.5 h-1.5 rounded-full ${addon.is_enabled ? 'bg-emerald-500 animate-pulse' : 'bg-slate-300'}`} />
          {isComingSoon ? 'قريباً' : addon.is_enabled ? 'مفعّلة' : 'غير مفعّلة'}
        </div>

        {/* Actions */}
        <div className="flex items-center gap-2">
          {/* Toggle */}
          <button
            onClick={handleToggle}
            disabled={isComingSoon || toggling}
            className={`flex-1 flex items-center justify-center gap-2 py-2 text-sm font-medium rounded-xl border transition-colors ${
              isComingSoon
                ? 'bg-slate-50 border-slate-200 text-slate-400 cursor-not-allowed'
                : addon.is_enabled
                  ? 'bg-red-50 border-red-200 text-red-600 hover:bg-red-100'
                  : 'bg-brand-50 border-brand-200 text-brand-700 hover:bg-brand-100'
            }`}
          >
            {toggling ? (
              <Loader2 className="w-4 h-4 animate-spin" />
            ) : addon.is_enabled ? (
              <><ToggleRight className="w-4 h-4" /> تعطيل</>
            ) : (
              <><ToggleLeft className="w-4 h-4" /> تفعيل</>
            )}
          </button>

          {/* Settings */}
          {addon.has_settings && !isComingSoon && (
            <button
              onClick={() => onSettings(addon)}
              title="إعدادات الإضافة"
              className="flex items-center justify-center p-2 rounded-xl border border-slate-200 text-slate-500 hover:bg-slate-50 hover:text-brand-600 hover:border-brand-200 transition-colors"
            >
              <Settings2 className="w-4 h-4" />
            </button>
          )}
        </div>
      </div>
    </div>
  )
}

// ── Main page ─────────────────────────────────────────────────────────────────

export default function MerchantAddons() {
  const [addons,  setAddons]  = useState<AddonItem[]>([])
  const [loading, setLoading] = useState(true)
  const [error,   setError]   = useState<string | null>(null)
  const [modal,   setModal]   = useState<AddonItem | null>(null)

  const loadAddons = useCallback(async () => {
    setLoading(true); setError(null)
    try {
      const { addons } = await addonsApi.list()
      setAddons(addons)
    } catch {
      setError('تعذّر تحميل الإضافات — تحقق من الاتصال وحاول مجدداً')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { loadAddons() }, [loadAddons])

  const handleToggled = (updated: AddonItem) =>
    setAddons(prev => prev.map(a => a.key === updated.key ? updated : a))

  const handleSaved = (updated: AddonItem) =>
    setAddons(prev => prev.map(a => a.key === updated.key ? updated : a))

  const handleSettings = (addon: AddonItem) => setModal(addon)

  const activeCount = addons.filter(a => a.is_enabled).length

  return (
    <div className="space-y-6">
      {/* ── Page header ── */}
      <div className="flex items-start justify-between gap-4">
        <div>
          <h1 className="text-xl font-bold text-slate-800">إضافات المتجر</h1>
          <p className="text-sm text-slate-500 mt-1">
            فعّل أدوات إضافية لمتجرك وخصّصها بسهولة
          </p>
        </div>
        {!loading && addons.length > 0 && (
          <div className="flex items-center gap-2 px-3 py-1.5 bg-brand-50 border border-brand-200 rounded-xl text-xs font-medium text-brand-700">
            <Zap className="w-3.5 h-3.5" />
            {activeCount} من {addons.length} مفعّلة
          </div>
        )}
      </div>

      {/* ── States ── */}
      {loading && (
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4">
          {[1, 2, 3].map(i => (
            <div key={i} className="bg-white rounded-2xl border border-slate-200 p-5 animate-pulse space-y-3">
              <div className="flex justify-between">
                <div className="w-10 h-10 bg-slate-100 rounded-xl" />
                <div className="w-16 h-5 bg-slate-100 rounded-full" />
              </div>
              <div className="w-3/4 h-4 bg-slate-100 rounded" />
              <div className="w-full h-3 bg-slate-100 rounded" />
              <div className="w-2/3 h-3 bg-slate-100 rounded" />
              <div className="h-9 bg-slate-100 rounded-xl" />
            </div>
          ))}
        </div>
      )}

      {!loading && error && (
        <div className="flex items-center gap-3 p-5 bg-red-50 border border-red-200 rounded-2xl text-sm text-red-700">
          <AlertCircle className="w-5 h-5 shrink-0" />
          <div>
            <p className="font-medium">{error}</p>
            <button onClick={loadAddons} className="mt-1 text-red-600 underline underline-offset-2 hover:text-red-800">
              إعادة المحاولة
            </button>
          </div>
        </div>
      )}

      {!loading && !error && addons.length === 0 && (
        <div className="text-center py-16 text-slate-400">
          <Puzzle className="w-10 h-10 mx-auto mb-3 opacity-40" />
          <p className="text-sm">لا توجد إضافات متاحة حالياً</p>
        </div>
      )}

      {/* ── Addons grid ── */}
      {!loading && !error && addons.length > 0 && (
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4">
          {addons.map(addon => (
            <AddonCard
              key={addon.key}
              addon={addon}
              onToggled={handleToggled}
              onSettings={handleSettings}
            />
          ))}
        </div>
      )}

      {/* ── Coming soon note ── */}
      {!loading && !error && (
        <div className="flex items-center gap-2 p-4 bg-slate-50 border border-slate-200 rounded-2xl text-xs text-slate-500">
          <Puzzle className="w-4 h-4 shrink-0 text-slate-400" />
          المزيد من الإضافات قادمة قريباً — نافذة السلات المتروكة، شريط العروض، والمزيد.
        </div>
      )}

      {/* ── Settings modal ── */}
      {modal && (
        <SettingsModal
          addon={modal}
          onClose={() => setModal(null)}
          onSaved={updated => { handleSaved(updated); setModal(null) }}
        />
      )}
    </div>
  )
}
