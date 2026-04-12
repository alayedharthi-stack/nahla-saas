import { useState, useEffect, useCallback } from 'react'
import { Link } from 'react-router-dom'
import {
  Save, Bot, Store, Users, Bell, MessageSquare,
  CheckCircle, AlertCircle, Loader2, Copy, ExternalLink,
  Eye, EyeOff, RefreshCw, UserPlus, Shield, ShieldOff, ToggleLeft, ToggleRight,
  Sparkles, Play, Zap, ShoppingCart, RotateCcw, Heart, Package,
  ChevronDown, ChevronUp, Clock, BrainCircuit, ShieldCheck, ExternalLink as LinkOut,
  Wifi, WifiOff, BadgeCheck, RefreshCw as ReconnectIcon, Code2,
} from 'lucide-react'
import { useLanguage } from '../i18n/context'
import { settingsApi, type AllSettings, type WhatsAppSettings, type AISettings, type StoreSettings, type NotificationSettings } from '../api/settings'
import { apiCall, API_BASE } from '../api/client'
import { whatsappConnectApi, type WaConnection } from '../api/whatsappConnect'
import {
  autopilotApi,
  type AutopilotStatus,
  type AutopilotSettings,
  AUTOPILOT_SUB_META,
} from '../api/autopilot'
import {
  aiSalesApi,
  type AiSalesAgentSettings,
  AI_SALES_PERMISSION_META,
} from '../api/aiSalesAgent'

// ── Primitives ──────────────────────────────────────────────────────────────

function Section({ title, description, children }: { title: string; description?: string; children: React.ReactNode }) {
  return (
    <div className="card">
      <div className="px-5 py-4 border-b border-slate-100">
        <h2 className="text-sm font-semibold text-slate-900">{title}</h2>
        {description && <p className="text-xs text-slate-400 mt-0.5">{description}</p>}
      </div>
      <div className="p-5">{children}</div>
    </div>
  )
}

function Field({ label, hint, children }: { label: string; hint?: string; children: React.ReactNode }) {
  return (
    <div>
      <label className="label">{label}</label>
      {children}
      {hint && <p className="text-xs text-slate-400 mt-1">{hint}</p>}
    </div>
  )
}

function Toggle({
  label, hint, value, onChange,
}: { label: string; hint?: string; value: boolean; onChange: (v: boolean) => void }) {
  return (
    <div className="flex items-start justify-between py-3 border-b border-slate-50 last:border-0">
      <div>
        <p className="text-sm text-slate-800">{label}</p>
        {hint && <p className="text-xs text-slate-400 mt-0.5">{hint}</p>}
      </div>
      <button onClick={() => onChange(!value)} className="ms-4 shrink-0">
        {value
          ? <ToggleRight className="w-6 h-6 text-brand-500" />
          : <ToggleLeft  className="w-6 h-6 text-slate-300" />}
      </button>
    </div>
  )
}

function SecretInput({ value, onChange, placeholder }: { value: string; onChange: (v: string) => void; placeholder?: string }) {
  const [show, setShow] = useState(false)
  return (
    <div className="relative">
      <input
        type={show ? 'text' : 'password'}
        className="input pe-10"
        value={value}
        onChange={e => onChange(e.target.value)}
        placeholder={placeholder}
        dir="ltr"
      />
      <button
        type="button"
        className="absolute inset-y-0 end-0 pe-3 flex items-center text-slate-400 hover:text-slate-600"
        onClick={() => setShow(s => !s)}
      >
        {show ? <EyeOff className="w-4 h-4" /> : <Eye className="w-4 h-4" />}
      </button>
    </div>
  )
}

function ReadonlyInput({ value, copyable }: { value: string; copyable?: boolean }) {
  const [copied, setCopied] = useState(false)
  const copy = () => {
    navigator.clipboard.writeText(value)
    setCopied(true)
    setTimeout(() => setCopied(false), 2000)
  }
  return (
    <div className="relative">
      <input
        readOnly
        className="input pe-10 bg-slate-50 text-slate-500 cursor-default"
        value={value}
        dir="ltr"
      />
      {copyable && (
        <button
          type="button"
          className="absolute inset-y-0 end-0 pe-3 flex items-center text-slate-400 hover:text-slate-600"
          onClick={copy}
        >
          {copied ? <CheckCircle className="w-4 h-4 text-emerald-500" /> : <Copy className="w-4 h-4" />}
        </button>
      )}
    </div>
  )
}

function SaveBar({
  onSave, saving, saved, error,
}: { onSave: () => void; saving: boolean; saved: boolean; error: string | null }) {
  const { t } = useLanguage()
  return (
    <div className="flex items-center gap-3 flex-wrap">
      <button onClick={onSave} disabled={saving} className="btn-primary text-sm">
        {saving
          ? <Loader2 className="w-4 h-4 animate-spin" />
          : <Save className="w-4 h-4" />}
        {saving ? t(tr => tr.settings.saveBar.saving) : t(tr => tr.settings.saveBar.save)}
      </button>
      {saved && (
        <span className="flex items-center gap-1.5 text-sm text-emerald-600">
          <CheckCircle className="w-4 h-4" /> {t(tr => tr.settings.saveBar.saved)}
        </span>
      )}
      {error && (
        <span className="flex items-center gap-1.5 text-sm text-red-600">
          <AlertCircle className="w-4 h-4" /> {error}
        </span>
      )}
    </div>
  )
}

// ── Tab definitions ─────────────────────────────────────────────────────────

const TAB_IDS = ['whatsapp', 'ai', 'automation', 'ai_sales', 'store', 'team', 'notifications', 'security', 'widget'] as const
type TabId = typeof TAB_IDS[number]

const TAB_ICONS: Record<TabId, React.ComponentType<{ className?: string }>> = {
  whatsapp:      MessageSquare,
  ai:            Bot,
  automation:    Sparkles,
  ai_sales:      BrainCircuit,
  store:         Store,
  team:          Users,
  notifications: Bell,
  security:      ShieldCheck,
  widget:        Code2,
}

// ── Tab: WhatsApp ────────────────────────────────────────────────────────────

function WhatsAppConnectionCard({ onConnected }: { onConnected?: (v: boolean) => void } = {}) {
  const [conn, setConn] = useState<WaConnection | null>(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    whatsappConnectApi.getStatus()
      .then(c => { setConn(c); onConnected?.(c?.connected === true) })
      .catch(() => { setConn(null); onConnected?.(false) })
      .finally(() => setLoading(false))
  }, []) // eslint-disable-line react-hooks/exhaustive-deps

  const isConnected = conn?.connected === true
  const needsReauth = conn?.status === 'needs_reauth' || conn?.status === 'error'

  if (loading) {
    return (
      <div className="flex items-center gap-2 py-4 text-slate-400 text-sm">
        <Loader2 className="w-4 h-4 animate-spin" />
        جاري التحقق من حالة الاتصال...
      </div>
    )
  }

  return (
    <div className={`rounded-xl border p-4 ${
      isConnected
        ? 'bg-emerald-50 border-emerald-200'
        : needsReauth
          ? 'bg-amber-50 border-amber-200'
          : 'bg-slate-50 border-slate-200'
    }`}>
      <div className="flex items-start justify-between gap-3 flex-wrap">
        <div className="flex items-center gap-3">
          <div className={`w-10 h-10 rounded-xl flex items-center justify-center ${
            isConnected ? 'bg-emerald-100' : 'bg-white/70'
          }`}>
            {isConnected
              ? <BadgeCheck className="w-5 h-5 text-emerald-600" />
              : <WifiOff className="w-5 h-5 text-slate-400" />
            }
          </div>
          <div>
            <p className={`text-sm font-bold ${
              isConnected ? 'text-emerald-800'
              : needsReauth ? 'text-amber-800'
              : 'text-slate-600'
            }`}>
              {isConnected
                ? 'متصل بواتساب ✅'
                : needsReauth
                  ? 'انتهت صلاحية الاتصال ⚠️'
                  : 'غير متصل بواتساب'
              }
            </p>
            {isConnected && conn?.phone_number && (
              <p className="text-xs font-mono text-emerald-700 mt-0.5 dir-ltr">
                {conn.business_display_name
                  ? `${conn.business_display_name} · ${conn.phone_number}`
                  : conn.phone_number
                }
              </p>
            )}
            {!isConnected && (
              <p className="text-xs text-slate-500 mt-0.5">
                اربط رقم واتساب للأعمال لتفعيل الردود التلقائية
              </p>
            )}
          </div>
        </div>

        <Link
          to="/whatsapp-connect"
          className={`flex items-center gap-2 text-sm font-bold px-4 py-2 rounded-xl transition-all ${
            isConnected
              ? 'bg-white border border-emerald-300 text-emerald-700 hover:bg-emerald-50'
              : needsReauth
                ? 'bg-amber-500 text-white hover:bg-amber-400'
                : 'bg-emerald-600 text-white hover:bg-emerald-500 shadow-lg shadow-emerald-600/20'
          }`}
        >
          {isConnected
            ? <><ReconnectIcon className="w-4 h-4" /> إعادة ربط واتساب</>
            : <><MessageSquare className="w-4 h-4" /> ربط واتساب</>
          }
        </Link>
      </div>

      {isConnected && conn?.connected_at && (
        <p className="text-xs text-emerald-600/70 mt-3 border-t border-emerald-200 pt-2">
          تم الربط في {new Date(conn.connected_at).toLocaleDateString('ar-SA')}
          {conn.whatsapp_business_account_id && (
            <span className="ms-3 font-mono">WABA: {conn.whatsapp_business_account_id}</span>
          )}
        </p>
      )}
    </div>
  )
}


function WhatsAppTab({
  data, onChange, onSave, saving, saved, saveError,
}: {
  data: WhatsAppSettings
  onChange: (patch: Partial<WhatsAppSettings>) => void
  onSave: () => void
  saving: boolean
  saved: boolean
  saveError: string | null
}) {
  const { t } = useLanguage()
  const s = t(tr => tr.settings.whatsapp)
  const [testing, setTesting] = useState(false)
  const [testResult, setTestResult] = useState<{ success: boolean; message: string } | null>(null)
  const [webhookOpen, setWebhookOpen] = useState(false)
  const [waConnected, setWaConnected] = useState(false)

  useEffect(() => {
    whatsappConnectApi.getStatus()
      .then(c => setWaConnected(c?.connected === true))
      .catch(() => setWaConnected(false))
  }, [])

  const handleTest = async () => {
    setTesting(true)
    setTestResult(null)
    try {
      const result = await settingsApi.testWhatsApp()
      setTestResult(result)
    } catch {
      setTestResult({ success: false, message: s.testFail })
    } finally {
      setTesting(false)
    }
  }

  return (
    <div className="space-y-5">

      {/* ── 1. Connection status (no technical fields) ── */}
      <Section
        title="حالة الاتصال بواتساب للأعمال"
        description="اربط رقم واتساب للأعمال الخاص بمتجرك بخطوة واحدة"
      >
        <WhatsAppConnectionCard onConnected={setWaConnected} />
        <p className="text-xs text-slate-400 mt-3 flex items-start gap-1.5">
          <Shield className="w-3.5 h-3.5 shrink-0 mt-0.5 text-slate-400" />
          جميع بيانات الاتصال (Phone Number ID، Access Token) تُحفظ بشكل آمن على الخادم
          ولا تظهر في الواجهة.
        </p>
      </Section>

      {/* ── 2+ Settings: only shown when WhatsApp is connected ── */}
      {!waConnected && (
        <div className="rounded-xl border border-dashed border-slate-200 bg-slate-50 p-6 text-center space-y-2">
          <MessageSquare className="w-8 h-8 text-slate-300 mx-auto" />
          <p className="text-sm font-medium text-slate-500">اربط واتساب أولاً لتفعيل هذه الإعدادات</p>
          <p className="text-xs text-slate-400">بعد الربط ستظهر إعدادات الإشعارات والردود التلقائية</p>
        </div>
      )}

      {waConnected && (<>
      <Section
        title={s.buttonsTitle}
        description={s.buttonsDesc}
      >
        <div className="grid sm:grid-cols-2 gap-4">
          <Field label={s.ownerWhatsapp} hint="رقم الواتساب الذي تصله إشعارات المنصة (مثل: تم الدفع، اشتراك جديد)">
            <input
              className="input"
              value={data.owner_whatsapp_number}
              onChange={e => onChange({ owner_whatsapp_number: e.target.value })}
              placeholder="+966 50 000 0000"
              dir="ltr"
            />
          </Field>
          <Field label={s.storeBtnLabel} hint="نص زر رابط المتجر في محادثات واتساب">
            <input
              className="input"
              value={data.store_button_label}
              onChange={e => onChange({ store_button_label: e.target.value })}
            />
          </Field>
          <Field label={s.storeBtnUrl}>
            <input
              className="input"
              value={data.store_button_url}
              onChange={e => onChange({ store_button_url: e.target.value })}
              placeholder="https://..."
              dir="ltr"
            />
          </Field>
          <Field label={s.ownerBtnLabel}>
            <input
              className="input"
              value={data.owner_contact_label}
              onChange={e => onChange({ owner_contact_label: e.target.value })}
            />
          </Field>
        </div>
      </Section>

      {/* ── 3. Auto-reply ── */}
      <Section title={s.autoReplyTitle}>
        <Toggle
          label={s.autoReplyLabel}
          hint={s.autoReplyHint}
          value={data.auto_reply_enabled}
          onChange={v => onChange({ auto_reply_enabled: v })}
        />
        <Toggle
          label={s.transferLabel}
          hint={s.transferHint}
          value={data.transfer_to_owner_enabled}
          onChange={v => onChange({ transfer_to_owner_enabled: v })}
        />
      </Section>

      {/* ── 4. Webhook reference (collapsed by default) ── */}
      <div className="rounded-xl border border-slate-200 overflow-hidden">
        <button
          onClick={() => setWebhookOpen(v => !v)}
          className="w-full flex items-center justify-between px-4 py-3 bg-slate-50 hover:bg-slate-100 transition-colors"
        >
          <span className="flex items-center gap-2 text-sm font-medium text-slate-600">
            <ShieldCheck className="w-4 h-4 text-slate-400" />
            إعدادات Webhook (للمطورين فقط)
          </span>
          {webhookOpen
            ? <ChevronUp className="w-4 h-4 text-slate-400" />
            : <ChevronDown className="w-4 h-4 text-slate-400" />
          }
        </button>

        {webhookOpen && (
          <div className="p-4 space-y-4 border-t border-slate-100">
            <div className="grid sm:grid-cols-2 gap-4">
              <Field label="Webhook URL" hint={s.webhookHint}>
                <ReadonlyInput
                  value={data.webhook_url || 'https://api.nahlah.ai/webhook/whatsapp'}
                  copyable
                />
              </Field>
              <Field label="Verify Token" hint={s.verifyHint}>
                <SecretInput
                  value={data.verify_token}
                  onChange={v => onChange({ verify_token: v })}
                  placeholder="nahla_secret_token"
                />
              </Field>
            </div>
            <div className="p-3 bg-blue-50 rounded-lg border border-blue-100">
              <p className="text-xs text-blue-700 flex items-start gap-2">
                <ExternalLink className="w-3.5 h-3.5 shrink-0 mt-0.5" />
                {s.webhookNote}
              </p>
            </div>
          </div>
        )}
      </div>

      {/* ── Save bar + test ── */}
      <div className="flex items-center gap-3 flex-wrap">
        <button onClick={handleTest} disabled={testing} className="btn-secondary text-sm">
          {testing ? <Loader2 className="w-4 h-4 animate-spin" /> : <RefreshCw className="w-4 h-4" />}
          {testing ? s.testingBtn : s.testBtn}
        </button>
        {testResult && (
          <span className={`flex items-center gap-1.5 text-sm ${testResult.success ? 'text-emerald-600' : 'text-red-600'}`}>
            {testResult.success ? <CheckCircle className="w-4 h-4" /> : <AlertCircle className="w-4 h-4" />}
            {testResult.message}
          </span>
        )}
      </div>

      <SaveBar onSave={onSave} saving={saving} saved={saved} error={saveError} />
      </>)} {/* end waConnected */}
    </div>
  )
}

// ── Tab: AI ──────────────────────────────────────────────────────────────────

function AITab({
  data, onChange, onSave, saving, saved, saveError,
}: {
  data: AISettings
  onChange: (patch: Partial<AISettings>) => void
  onSave: () => void
  saving: boolean
  saved: boolean
  saveError: string | null
}) {
  const { t } = useLanguage()
  const s = t(tr => tr.settings.ai)
  return (
    <div className="space-y-5">
      <Section title={s.personalityTitle} description={s.personalityDesc}>
        <div className="grid sm:grid-cols-2 gap-4">
          <Field label={s.assistantName}>
            <input className="input" value={data.assistant_name} onChange={e => onChange({ assistant_name: e.target.value })} placeholder="نحلة / Nahlah" />
          </Field>
          <Field label={s.replyTone}>
            <select className="input" value={data.reply_tone} onChange={e => onChange({ reply_tone: e.target.value as AISettings['reply_tone'] })}>
              <option value="friendly">{s.toneOptions.friendly}</option>
              <option value="professional">{s.toneOptions.formal}</option>
              <option value="sales">{s.toneOptions.luxury}</option>
            </select>
          </Field>
          <Field label={t(tr => tr.meta.code === 'ar' ? 'طول الرد' : 'Reply Length')}>
            <select className="input" value={data.reply_length} onChange={e => onChange({ reply_length: e.target.value as AISettings['reply_length'] })}>
              <option value="short">{t(tr => tr.meta.code === 'ar' ? 'قصير ومختصر' : 'Short & concise')}</option>
              <option value="medium">{t(tr => tr.meta.code === 'ar' ? 'متوسط' : 'Medium')}</option>
              <option value="detailed">{t(tr => tr.meta.code === 'ar' ? 'تفصيلي وشامل' : 'Detailed & comprehensive')}</option>
            </select>
          </Field>
          <Field label={s.languageLabel}>
            <select className="input" value={data.default_language} onChange={e => onChange({ default_language: e.target.value as AISettings['default_language'] })}>
              <option value="arabic">{s.langOptions.arabic}</option>
              <option value="english">{s.langOptions.english}</option>
              <option value="bilingual">{s.langOptions.both}</option>
            </select>
          </Field>
          <div className="sm:col-span-2">
            <Field label="دور ووصف المساعدة" hint="يُقرأ بواسطة الذكاء الاصطناعي لفهم طبيعة المتجر">
              <textarea className="input min-h-[80px] resize-y" value={data.assistant_role} onChange={e => onChange({ assistant_role: e.target.value })} placeholder="مثال: أنت مساعدة لمتجر ملابس رجالية فاخرة في الرياض. تُجيب بلهجة ودية ومحترفة..." />
            </Field>
          </div>
        </div>
      </Section>

      <Section
        title={t(tr => tr.meta.code === 'ar' ? 'تعليمات المالك' : 'Owner Instructions')}
        description={t(tr => tr.meta.code === 'ar' ? 'تُضاف إلى كل محادثة كقواعد أساسية لنحلة' : 'Added to every conversation as base rules for Nahlah')}
      >
        <div className="space-y-4">
          <Field
            label={t(tr => tr.meta.code === 'ar' ? 'تعليمات عامة' : 'General Instructions')}
            hint={t(tr => tr.meta.code === 'ar' ? 'قواعد وسياسات يجب أن تلتزم بها نحلة دائماً' : 'Rules and policies Nahlah must always follow')}
          >
            <textarea className="input min-h-[100px] resize-y" value={data.owner_instructions} onChange={e => onChange({ owner_instructions: e.target.value })} placeholder={t(tr => tr.settings.ai.storePolicyPh)} />
          </Field>
          <Field
            label={t(tr => tr.meta.code === 'ar' ? 'قواعد تقديم الكوبونات' : 'Coupon Rules')}
            hint={t(tr => tr.meta.code === 'ar' ? 'متى تعرض نحلة خصماً على العميل' : 'When Nahlah should offer a discount')}
          >
            <textarea className="input min-h-[80px] resize-y" value={data.coupon_rules} onChange={e => onChange({ coupon_rules: e.target.value })} />
          </Field>
          <Field
            label={t(tr => tr.meta.code === 'ar' ? 'قواعد التصعيد للإنسان' : 'Escalation Rules')}
            hint={t(tr => tr.meta.code === 'ar' ? 'متى تحوّل نحلة المحادثة للمالك أو الدعم' : 'When Nahlah should transfer to human support')}
          >
            <textarea className="input min-h-[80px] resize-y" value={data.escalation_rules} onChange={e => onChange({ escalation_rules: e.target.value })} placeholder={t(tr => tr.settings.ai.handoffMsgPh)} />
          </Field>
        </div>
      </Section>

      <Section title={t(tr => tr.meta.code === 'ar' ? 'الخصومات والتوصيات' : 'Discounts & Recommendations')}>
        <div className="space-y-4">
          <Field label={t(tr => tr.meta.code === 'ar' ? 'الحد الأقصى للخصم المسموح به' : 'Max Allowed Discount')}>
            <select className="input" value={data.allowed_discount_levels} onChange={e => onChange({ allowed_discount_levels: e.target.value })}>
              <option value="0">{t(tr => tr.meta.code === 'ar' ? 'بدون خصم' : 'No discount')}</option>
              <option value="5">5%</option>
              <option value="10">10%</option>
              <option value="15">15%</option>
              <option value="20">20%</option>
              <option value="30">30%</option>
            </select>
          </Field>
          <Toggle
            label={t(tr => tr.meta.code === 'ar' ? 'تفعيل توصيات المنتجات' : 'Enable Product Recommendations')}
            hint={t(tr => tr.meta.code === 'ar' ? 'نحلة تقترح منتجات ذات صلة أثناء المحادثة' : 'Nahlah suggests related products during conversation')}
            value={data.recommendations_enabled}
            onChange={v => onChange({ recommendations_enabled: v })}
          />
        </div>

        <div className="mt-4 p-3 bg-amber-50 rounded-lg border border-amber-200">
          <p className="text-xs text-amber-700 flex items-start gap-2">
            <Bot className="w-3.5 h-3.5 shrink-0 mt-0.5" />
            {t(tr => tr.meta.code === 'ar'
              ? 'التغييرات تُطبَّق فوراً على المحادثات الجديدة. المحادثات الجارية لا تتأثر.'
              : 'Changes apply immediately to new conversations. Ongoing conversations are not affected.')}
          </p>
        </div>
      </Section>

      <SaveBar onSave={onSave} saving={saving} saved={saved} error={saveError} />
    </div>
  )
}

// ── Tab: Store & Integrations ────────────────────────────────────────────────

function StoreTab({
  data, onChange, onSave, saving, saved, saveError,
}: {
  data: StoreSettings
  onChange: (patch: Partial<StoreSettings>) => void
  onSave: () => void
  saving: boolean
  saved: boolean
  saveError: string | null
}) {
  const { t } = useLanguage()
  const s = t(tr => tr.settings.store)
  const isAr = t(tr => tr.meta.code) === 'ar'
  return (
    <div className="space-y-5">
      <Section title={s.title}>
        <div className="grid sm:grid-cols-2 gap-4">
          <Field label={s.nameLabel}>
            <input className="input" value={data.store_name} onChange={e => onChange({ store_name: e.target.value })} placeholder={isAr ? 'متجر أحمد للملابس' : "Ahmed's Fashion Store"} />
          </Field>
          <Field label={s.domainLabel}>
            <input className="input" value={data.store_url} onChange={e => onChange({ store_url: e.target.value })} placeholder="https://your-store.salla.sa" dir="ltr" />
          </Field>
          <div className="sm:col-span-2">
            <Field label={isAr ? 'رابط شعار المتجر' : 'Store Logo URL'} hint={isAr ? 'رابط مباشر لصورة الشعار (PNG أو SVG)' : 'Direct link to your logo image (PNG or SVG)'}>
              <input className="input" value={data.store_logo_url} onChange={e => onChange({ store_logo_url: e.target.value })} placeholder="https://cdn.example.com/logo.png" dir="ltr" />
            </Field>
          </div>
        </div>
      </Section>

      <Section title={isAr ? 'المنصة والتكامل' : 'Platform & Integration'} description={isAr ? 'اختر منصة متجرك وأدخل بيانات الربط' : 'Choose your store platform and enter connection details'}>
        <div className="space-y-4">
          <Field label={isAr ? 'نوع المنصة' : 'Platform Type'}>
            <select className="input" value={data.platform_type} onChange={e => onChange({ platform_type: e.target.value as StoreSettings['platform_type'] })}>
              <option value="salla">Salla {isAr ? '– سلة' : ''}</option>
              <option value="zid">Zid {isAr ? '– زد' : ''}</option>
              <option value="shopify">Shopify</option>
              <option value="custom">{isAr ? 'منصة مخصصة' : 'Custom platform'}</option>
            </select>
          </Field>

          {data.platform_type === 'salla' && (
            <div className="grid sm:grid-cols-2 gap-4 p-4 bg-slate-50 rounded-lg border border-slate-100">
              <p className="sm:col-span-2 text-xs font-medium text-slate-500 uppercase tracking-wide">Salla {isAr ? 'بيانات' : 'Credentials'}</p>
              <Field label="Client ID">
                <input className="input" value={data.salla_client_id} onChange={e => onChange({ salla_client_id: e.target.value })} placeholder="salla_client_id" dir="ltr" />
              </Field>
              <Field label="Client Secret">
                <SecretInput value={data.salla_client_secret} onChange={v => onChange({ salla_client_secret: v })} placeholder="salla_client_secret" />
              </Field>
              <div className="sm:col-span-2">
                <Field label="Access Token" hint={isAr ? 'من إعدادات التطبيق في لوحة سلة للمطورين' : 'From Salla Developer Dashboard → App Settings'}>
                  <SecretInput value={data.salla_access_token} onChange={v => onChange({ salla_access_token: v })} placeholder="Bearer token..." />
                </Field>
              </div>
            </div>
          )}

          {data.platform_type === 'zid' && (
            <div className="grid sm:grid-cols-2 gap-4 p-4 bg-slate-50 rounded-lg border border-slate-100">
              <p className="sm:col-span-2 text-xs font-medium text-slate-500 uppercase tracking-wide">Zid {isAr ? 'بيانات' : 'Credentials'}</p>
              <Field label="Client ID">
                <input className="input" value={data.zid_client_id} onChange={e => onChange({ zid_client_id: e.target.value })} placeholder="zid_client_id" dir="ltr" />
              </Field>
              <Field label="Client Secret">
                <SecretInput value={data.zid_client_secret} onChange={v => onChange({ zid_client_secret: v })} placeholder="zid_client_secret" />
              </Field>
            </div>
          )}

          {data.platform_type === 'shopify' && (
            <div className="grid sm:grid-cols-2 gap-4 p-4 bg-slate-50 rounded-lg border border-slate-100">
              <p className="sm:col-span-2 text-xs font-medium text-slate-500 uppercase tracking-wide">Shopify {isAr ? 'بيانات' : 'Credentials'}</p>
              <Field label="Shop Domain">
                <input className="input" value={data.shopify_shop_domain} onChange={e => onChange({ shopify_shop_domain: e.target.value })} placeholder="your-store.myshopify.com" dir="ltr" />
              </Field>
              <Field label="Admin API Access Token">
                <SecretInput value={data.shopify_access_token} onChange={v => onChange({ shopify_access_token: v })} placeholder="shpat_..." />
              </Field>
            </div>
          )}
        </div>
      </Section>

      <Section title={isAr ? 'الشحن والموقع' : 'Shipping & Location'}>
        <div className="grid sm:grid-cols-2 gap-4">
          <Field label={isAr ? 'شركة الشحن' : 'Shipping Provider'} hint={isAr ? 'اسم أو رابط واجهة API لشركة الشحن' : 'Name or API link of your shipping provider'}>
            <input className="input" value={data.shipping_provider} onChange={e => onChange({ shipping_provider: e.target.value })} placeholder={isAr ? 'أرامكس، سمسا...' : 'Aramex, SMSA...'} />
          </Field>
          <Field label={isAr ? 'رابط الموقع على خرائط قوقل' : 'Google Maps Location'}>
            <input className="input" value={data.google_maps_location} onChange={e => onChange({ google_maps_location: e.target.value })} placeholder="https://maps.google.com/..." dir="ltr" />
          </Field>
        </div>
      </Section>

      <Section title={isAr ? 'روابط التواصل الاجتماعي' : 'Social Media Links'}>
        <div className="grid sm:grid-cols-2 gap-4">
          <Field label="Instagram">
            <input className="input" value={data.instagram_url} onChange={e => onChange({ instagram_url: e.target.value })} placeholder="https://instagram.com/..." dir="ltr" />
          </Field>
          <Field label="X / Twitter">
            <input className="input" value={data.twitter_url} onChange={e => onChange({ twitter_url: e.target.value })} placeholder="https://x.com/..." dir="ltr" />
          </Field>
          <Field label="Snapchat">
            <input className="input" value={data.snapchat_url} onChange={e => onChange({ snapchat_url: e.target.value })} placeholder="https://snapchat.com/add/..." dir="ltr" />
          </Field>
          <Field label="TikTok">
            <input className="input" value={data.tiktok_url} onChange={e => onChange({ tiktok_url: e.target.value })} placeholder="https://tiktok.com/@..." dir="ltr" />
          </Field>
        </div>
      </Section>

      <SaveBar onSave={onSave} saving={saving} saved={saved} error={saveError} />
    </div>
  )
}

// ── Tab: Team ────────────────────────────────────────────────────────────────

const MOCK_TEAM = [
  { name: 'أحمد محمد',     email: 'ahmed@store.sa',   role: 'مالك',          roleKey: 'owner',   avatar: 'أ' },
  { name: 'سارة الزهراني', email: 'sara@store.sa',    role: 'مدير',          roleKey: 'admin',   avatar: 'س' },
  { name: 'خالد العمري',   email: 'khalid@store.sa',  role: 'دعم عملاء',     roleKey: 'support', avatar: 'خ' },
  { name: 'نورة السلمي',   email: 'noura@store.sa',   role: 'مدير تسويق',    roleKey: 'marketing', avatar: 'ن' },
]

const ROLE_BADGES: Record<string, string> = {
  owner:     'bg-brand-100 text-brand-700',
  admin:     'bg-purple-100 text-purple-700',
  support:   'bg-blue-100 text-blue-700',
  marketing: 'bg-emerald-100 text-emerald-700',
}

function TeamTab() {
  return (
    <div className="space-y-5">
      <Section
        title="أعضاء الفريق"
        description="إدارة المستخدمين وصلاحياتهم في لوحة نحلة"
      >
        <div className="space-y-2">
          {MOCK_TEAM.map(member => (
            <div key={member.email} className="flex items-center gap-3 py-3 border-b border-slate-50 last:border-0">
              <div className="w-9 h-9 rounded-full bg-brand-100 flex items-center justify-center text-sm font-bold text-brand-600 shrink-0">
                {member.avatar}
              </div>
              <div className="flex-1 min-w-0">
                <p className="text-sm font-medium text-slate-900">{member.name}</p>
                <p className="text-xs text-slate-400 truncate" dir="ltr">{member.email}</p>
              </div>
              <span className={`text-xs font-medium px-2 py-0.5 rounded-full ${ROLE_BADGES[member.roleKey]}`}>
                {member.role}
              </span>
              {member.roleKey !== 'owner' && (
                <button className="btn-ghost text-xs">تعديل</button>
              )}
            </div>
          ))}
        </div>

        <div className="mt-4 flex items-center gap-3">
          <button className="btn-primary text-sm">
            <UserPlus className="w-4 h-4" />
            دعوة عضو جديد
          </button>
        </div>
      </Section>

      <Section title="الصلاحيات حسب الدور" description="ملخص ما يستطيع كل دور فعله">
        <div className="overflow-x-auto -mx-1">
          <table className="w-full text-sm min-w-[500px]">
            <thead>
              <tr className="border-b border-slate-100">
                <th className="text-start py-2 px-2 text-xs font-medium text-slate-500 w-48">الصلاحية</th>
                <th className="text-center py-2 px-2 text-xs font-medium text-slate-500">مالك</th>
                <th className="text-center py-2 px-2 text-xs font-medium text-slate-500">مدير</th>
                <th className="text-center py-2 px-2 text-xs font-medium text-slate-500">دعم</th>
                <th className="text-center py-2 px-2 text-xs font-medium text-slate-500">تسويق</th>
              </tr>
            </thead>
            <tbody>
              {[
                { label: 'إعدادات النظام',      owner: true,  admin: false, support: false, marketing: false },
                { label: 'إدارة الفريق',         owner: true,  admin: true,  support: false, marketing: false },
                { label: 'عرض المحادثات',        owner: true,  admin: true,  support: true,  marketing: false },
                { label: 'إدارة الطلبات',        owner: true,  admin: true,  support: true,  marketing: false },
                { label: 'إنشاء الكوبونات',      owner: true,  admin: true,  support: false, marketing: true  },
                { label: 'إطلاق الحملات',        owner: true,  admin: true,  support: false, marketing: true  },
                { label: 'عرض التحليلات',        owner: true,  admin: true,  support: false, marketing: true  },
              ].map(row => (
                <tr key={row.label} className="border-b border-slate-50 last:border-0">
                  <td className="py-2 px-2 text-slate-700">{row.label}</td>
                  {(['owner', 'admin', 'support', 'marketing'] as const).map(role => (
                    <td key={role} className="py-2 px-2 text-center">
                      {(row as Record<string, unknown>)[role]
                        ? <CheckCircle className="w-4 h-4 text-emerald-500 mx-auto" />
                        : <span className="text-slate-200">—</span>}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>

        <div className="mt-4">
          <button className="btn-secondary text-sm">
            <Shield className="w-4 h-4" />
            تعديل الصلاحيات
          </button>
        </div>
      </Section>
    </div>
  )
}

// ── Tab: Notifications ───────────────────────────────────────────────────────

function NotificationsTab({
  data, onChange, onSave, saving, saved, saveError,
}: {
  data: NotificationSettings
  onChange: (patch: Partial<NotificationSettings>) => void
  onSave: () => void
  saving: boolean
  saved: boolean
  saveError: string | null
}) {
  const { t } = useLanguage()
  const s = t(tr => tr.settings.notifications)
  const isAr = t(tr => tr.meta.code) === 'ar'
  return (
    <div className="space-y-5">
      <Section title={s.whatsappEnabled} description={isAr ? 'تُرسَل مباشرة لرقم المالك على واتساب' : 'Sent directly to the owner\'s WhatsApp number'}>
        <Toggle label={s.whatsappEnabled} hint={s.whatsappHint} value={data.whatsapp_alerts} onChange={v => onChange({ whatsapp_alerts: v })} />
      </Section>

      <Section title={s.emailEnabled}>
        <Toggle label={s.emailEnabled} hint={s.emailHint} value={data.email_alerts} onChange={v => onChange({ email_alerts: v })} />
      </Section>

      <Section title={isAr ? 'تنبيهات النظام' : 'System Alerts'} description={isAr ? 'أحداث داخلية تؤثر على أداء نحلة' : 'Internal events affecting Nahlah performance'}>
        <Toggle
          label={isAr ? 'تنبيهات النظام العامة' : 'General System Alerts'}
          hint={isAr ? 'أخطاء، توقف الخدمة، استهلاك عالٍ' : 'Errors, service downtime, high usage'}
          value={data.system_alerts}
          onChange={v => onChange({ system_alerts: v })}
        />
        <Toggle
          label={isAr ? 'فشل Webhook' : 'Webhook Failures'}
          hint={isAr ? 'تنبيه عند فشل إرسال أو استقبال بيانات Webhook' : 'Alert when webhook send/receive fails'}
          value={data.failed_webhook_alerts}
          onChange={v => onChange({ failed_webhook_alerts: v })}
        />
        <Toggle
          label={isAr ? 'رصيد منخفض / مشكلة في API' : 'Low Balance / API Issue'}
          hint={isAr ? 'تنبيه عند قُرب نفاد رصيد API أو خطأ في المصادقة' : 'Alert when API credits are low or auth fails'}
          value={data.low_balance_alerts}
          onChange={v => onChange({ low_balance_alerts: v })}
        />
      </Section>

      <SaveBar onSave={onSave} saving={saving} saved={saved} error={saveError} />
    </div>
  )
}

// ── Autopilot Tab ────────────────────────────────────────────────────────────

const SUB_ICONS: Record<string, React.ReactNode> = {
  order_status_update: <Package     className="w-4 h-4 text-brand-500" />,
  predictive_reorder:  <RotateCcw   className="w-4 h-4 text-brand-500" />,
  abandoned_cart:      <ShoppingCart className="w-4 h-4 text-red-500"  />,
  inactive_recovery:   <Heart        className="w-4 h-4 text-blue-500"  />,
}

function SubAutomationCard({
  subKey,
  config,
  onToggle,
}: {
  subKey: keyof Omit<AutopilotSettings, 'enabled'>
  config: AutopilotSettings[typeof subKey]
  onToggle: (enabled: boolean) => void
}) {
  const [open, setOpen] = useState(false)
  const meta = AUTOPILOT_SUB_META[subKey]

  return (
    <div className={`border rounded-xl overflow-hidden transition-all ${config.enabled ? 'border-emerald-200 bg-emerald-50/30' : 'border-slate-100 bg-white'}`}>
      {/* Header */}
      <div className="flex items-start justify-between gap-3 p-4">
        <div className="flex items-start gap-3 min-w-0">
          <span className="text-xl leading-none mt-0.5 shrink-0">{meta.icon}</span>
          <div className="min-w-0">
            <div className="flex items-center gap-2 flex-wrap">
              <p className="text-sm font-semibold text-slate-900">{meta.label}</p>
              <span className={`text-[10px] px-1.5 py-0.5 rounded-full font-medium ${config.enabled ? 'bg-emerald-100 text-emerald-700' : 'bg-slate-100 text-slate-500'}`}>
                {config.enabled ? 'مُفعّل' : 'معطّل'}
              </span>
            </div>
            <p className="text-xs text-slate-500 mt-0.5 leading-relaxed">{meta.desc}</p>
            <div className="flex items-center gap-1.5 mt-1.5">
              <Zap className="w-2.5 h-2.5 text-slate-400" />
              <span className="text-[10px] font-mono text-slate-400">{meta.triggerLabel}</span>
              <span className="text-slate-300 mx-1">·</span>
              <span className="text-[10px] text-slate-400">قالب:</span>
              <span className="text-[10px] font-medium text-slate-600 bg-slate-100 px-1 py-0.5 rounded">{meta.template}</span>
            </div>
          </div>
        </div>
        <div className="flex items-center gap-2 shrink-0">
          <button
            type="button"
            onClick={() => setOpen(o => !o)}
            className="text-slate-400 hover:text-slate-600 p-1"
          >
            {open ? <ChevronUp className="w-4 h-4" /> : <ChevronDown className="w-4 h-4" />}
          </button>
          <button
            type="button"
            onClick={() => onToggle(!config.enabled)}
            className={`w-10 h-5 rounded-full transition-colors shrink-0 relative ${config.enabled ? 'bg-emerald-500' : 'bg-slate-200'}`}
          >
            <span className={`absolute top-0.5 h-4 w-4 rounded-full bg-white shadow transition-all ${config.enabled ? 'start-5' : 'start-0.5'}`} />
          </button>
        </div>
      </div>

      {/* Expanded detail */}
      {open && (
        <div className="border-t border-slate-100 bg-slate-50 px-4 py-3 space-y-2">
          {subKey === 'order_status_update' && (
            <>
              <p className="text-xs text-slate-600">
                <span className="font-medium">التدفق:</span> فور تغيُّر حالة الطلب يُرسل إشعار واتساب للعميل (انتظار، شحن، توصيل، إلغاء...).
              </p>
              <p className="text-xs text-slate-500">يُتابع جميع الطلبات تلقائياً دون تكرار.</p>
            </>
          )}
          {subKey === 'predictive_reorder' && (
            <>
              <p className="text-xs text-slate-600"><span className="font-medium">التدفق:</span> تحليل دورة الاستهلاك → إرسال تذكير قبل {(config as AutopilotSettings['predictive_reorder']).days_before} أيام من النفاد المتوقع</p>
              <p className="text-xs text-slate-500">دورة الاستهلاك الافتراضية: {(config as AutopilotSettings['predictive_reorder']).consumption_days_default} يوماً</p>
            </>
          )}
          {subKey === 'abandoned_cart' && (
            <>
              <div className="flex items-center gap-3 text-xs">
                <span className={`flex items-center gap-1 ${(config as AutopilotSettings['abandoned_cart']).reminder_30min ? 'text-emerald-600' : 'text-slate-400 line-through'}`}>
                  <Clock className="w-3 h-3" /> 30 دقيقة
                </span>
                <span className="text-slate-300">→</span>
                <span className={`flex items-center gap-1 ${(config as AutopilotSettings['abandoned_cart']).reminder_24h ? 'text-emerald-600' : 'text-slate-400 line-through'}`}>
                  <Clock className="w-3 h-3" /> 24 ساعة
                </span>
                <span className="text-slate-300">→</span>
                <span className={`flex items-center gap-1 ${(config as AutopilotSettings['abandoned_cart']).coupon_48h ? 'text-emerald-600' : 'text-slate-400 line-through'}`}>
                  <Clock className="w-3 h-3" /> 48 ساعة + كوبون
                </span>
              </div>
            </>
          )}
          {subKey === 'inactive_recovery' && (
            <p className="text-xs text-slate-600">
              <span className="font-medium">الشرط:</span> لم يتسوق منذ {(config as AutopilotSettings['inactive_recovery']).inactive_days} يوماً → عرض خصم {(config as AutopilotSettings['inactive_recovery']).discount_pct}%
            </p>
          )}
          <p className="text-[10px] text-blue-600 flex items-center gap-1 mt-1">
            <AlertCircle className="w-3 h-3 shrink-0" />
            يستخدم القالب المعتمد: <strong>{meta.template}</strong> — يجب أن يكون بحالة APPROVED قبل التفعيل.
          </p>
        </div>
      )}
    </div>
  )
}

function DailySummaryPanel({ items, lastRunAt, onRunNow, running }: {
  items: { key: string; label: string; count: number; icon: string }[]
  lastRunAt: string | null
  onRunNow: () => void
  running: boolean
}) {
  const total = items.reduce((s, i) => s + i.count, 0)

  const formatTime = (iso: string | null) => {
    if (!iso) return null
    try {
      return new Date(iso).toLocaleTimeString('ar-SA', { hour: '2-digit', minute: '2-digit' })
    } catch { return iso }
  }

  return (
    <div className="card overflow-hidden">
      <div className="px-5 py-4 border-b border-slate-100 flex items-center justify-between gap-3">
        <div>
          <h3 className="text-sm font-semibold text-slate-900">إجراءات نحلة اليوم</h3>
          {lastRunAt && (
            <p className="text-xs text-slate-400 mt-0.5">
              آخر تشغيل: {formatTime(lastRunAt)}
            </p>
          )}
        </div>
        <button
          onClick={onRunNow}
          disabled={running}
          className="btn-secondary text-xs flex items-center gap-1.5"
        >
          {running
            ? <Loader2 className="w-3.5 h-3.5 animate-spin" />
            : <Play   className="w-3.5 h-3.5" />}
          {running ? 'جارٍ التشغيل...' : 'تشغيل الآن'}
        </button>
      </div>

      {total === 0 ? (
        <div className="px-5 py-8 text-center">
          <Sparkles className="w-8 h-8 text-slate-200 mx-auto mb-2" />
          <p className="text-sm text-slate-400">لا توجد إجراءات اليوم بعد.</p>
          <p className="text-xs text-slate-300 mt-1">انقر "تشغيل الآن" لاختبار الطيار التلقائي.</p>
        </div>
      ) : (
        <div className="divide-y divide-slate-50">
          {items.map(item => (
            <div key={item.key} className="flex items-center gap-3 px-5 py-3">
              <span className="text-lg leading-none shrink-0">{item.icon}</span>
              <p className="flex-1 text-sm text-slate-700">{item.label}</p>
              <span className={`text-base font-bold ${item.count > 0 ? 'text-brand-600' : 'text-slate-300'}`}>
                {item.count}
              </span>
            </div>
          ))}
          <div className="flex items-center gap-3 px-5 py-3 bg-slate-50">
            <span className="text-lg leading-none shrink-0">📊</span>
            <p className="flex-1 text-sm font-semibold text-slate-700">الإجمالي</p>
            <span className="text-base font-bold text-brand-700">{total}</span>
          </div>
        </div>
      )}
    </div>
  )
}

function AutopilotTab() {
  const [status, setStatus]   = useState<AutopilotStatus | null>(null)
  const [loading, setLoading] = useState(true)
  const [saving,  setSaving]  = useState(false)
  const [running, setRunning] = useState(false)
  const [runResult, setRunResult] = useState<{ total: number; message: string } | null>(null)

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const s = await autopilotApi.status()
      setStatus(s)
    } catch { /* non-fatal */ }
    finally { setLoading(false) }
  }, [])

  useEffect(() => { load() }, [load])

  const toggleMaster = async (enabled: boolean) => {
    if (!status) return
    setStatus(s => s ? { ...s, settings: { ...s.settings, enabled } } : s)
    setSaving(true)
    try {
      const res = await autopilotApi.save({ enabled })
      setStatus(s => s ? { ...s, settings: res.settings } : s)
    } catch {
      setStatus(s => s ? { ...s, settings: { ...s.settings, enabled: !enabled } } : s)
    } finally { setSaving(false) }
  }

  const toggleSub = async (
    subKey: keyof Omit<AutopilotSettings, 'enabled'>,
    enabled: boolean,
  ) => {
    if (!status) return
    setStatus(s => s ? {
      ...s,
      settings: { ...s.settings, [subKey]: { ...s.settings[subKey], enabled } },
    } : s)
    try {
      const res = await autopilotApi.save({ [subKey]: { enabled } } as any)
      setStatus(s => s ? { ...s, settings: res.settings } : s)
    } catch {
      setStatus(s => s ? {
        ...s,
        settings: { ...s.settings, [subKey]: { ...s.settings[subKey], enabled: !enabled } },
      } : s)
    }
  }

  const handleRunNow = async () => {
    setRunning(true)
    setRunResult(null)
    try {
      const res = await autopilotApi.runNow()
      setRunResult({ total: res.total_actions, message: res.message })
      await load()
    } catch {
      setRunResult({ total: 0, message: 'تعذّر تشغيل الطيار التلقائي.' })
    } finally { setRunning(false) }
  }

  if (loading) {
    return (
      <div className="flex items-center justify-center py-20">
        <Loader2 className="w-6 h-6 animate-spin text-brand-500" />
        <span className="ms-3 text-sm text-slate-500">تحميل إعدادات الأتمتة...</span>
      </div>
    )
  }

  const ap = status?.settings ?? {
    enabled: false,
    order_status_update: { enabled: true, notify_statuses: ['pending', 'shipped', 'delivered', 'cancelled'], template_name: 'order_status_update_ar' },
    predictive_reorder:  { enabled: true, days_before: 3, consumption_days_default: 45, template_name: 'predictive_reorder_reminder_ar' },
    abandoned_cart:      { enabled: true, reminder_30min: true, reminder_24h: true, coupon_48h: false, coupon_code: '', template_name: 'abandoned_cart_reminder' },
    inactive_recovery:   { enabled: true, inactive_days: 60, discount_pct: 15, template_name: 'win_back' },
  }

  return (
    <div className="space-y-5">

      {/* ── Master toggle card ── */}
      <div className={`card p-5 transition-all ${ap.enabled ? 'ring-2 ring-emerald-300 ring-offset-1' : ''}`}>
        <div className="flex items-center justify-between gap-4">
          <div className="flex items-start gap-4">
            <div className={`w-12 h-12 rounded-xl flex items-center justify-center shrink-0 ${ap.enabled ? 'bg-emerald-100' : 'bg-slate-100'}`}>
              <Sparkles className={`w-6 h-6 ${ap.enabled ? 'text-emerald-600' : 'text-slate-400'}`} />
            </div>
            <div>
              <div className="flex items-center gap-2 flex-wrap">
                <h2 className="text-base font-bold text-slate-900">طيار المبيعات التلقائي</h2>
                <span className={`text-xs font-medium px-2 py-0.5 rounded-full ${ap.enabled ? 'bg-emerald-100 text-emerald-700' : 'bg-slate-100 text-slate-500'}`}>
                  {ap.enabled ? 'مُفعّل ✅' : 'معطّل'}
                </span>
              </div>
              <p className="text-sm text-slate-500 mt-1 leading-relaxed max-w-lg">
                عند التفعيل تتولى نحلة تلقائياً: تأكيد طلبات COD، تذكيرات إعادة الطلب، استرداد السلات المتروكة، واسترجاع العملاء الخاملين.
              </p>
            </div>
          </div>

          {/* Master toggle switch */}
          <button
            type="button"
            disabled={saving}
            onClick={() => toggleMaster(!ap.enabled)}
            className={`w-14 h-7 rounded-full transition-colors shrink-0 relative focus:outline-none focus:ring-2 focus:ring-offset-2 focus:ring-emerald-500 ${ap.enabled ? 'bg-emerald-500' : 'bg-slate-200'} ${saving ? 'opacity-60' : ''}`}
          >
            <span className={`absolute top-0.5 h-6 w-6 rounded-full bg-white shadow-md transition-all duration-200 ${ap.enabled ? 'translate-x-7' : 'translate-x-0.5'}`} />
          </button>
        </div>

        {/* Compliance notice */}
        <div className="mt-4 flex items-start gap-2.5 bg-blue-50 border border-blue-100 rounded-lg px-3.5 py-2.5">
          <AlertCircle className="w-3.5 h-3.5 text-blue-500 shrink-0 mt-0.5" />
          <p className="text-xs text-blue-700">
            جميع الرسائل التلقائية تستخدم قوالب واتساب معتمدة من Meta فقط. يمكن تعطيل أي أتمتة بشكل مستقل.
          </p>
        </div>
      </div>

      {/* ── Run result toast ── */}
      {runResult && (
        <div className={`flex items-start gap-3 px-4 py-3 rounded-xl border ${runResult.total > 0 ? 'bg-emerald-50 border-emerald-200' : 'bg-amber-50 border-amber-200'}`}>
          <CheckCircle className={`w-4 h-4 shrink-0 mt-0.5 ${runResult.total > 0 ? 'text-emerald-500' : 'text-amber-500'}`} />
          <p className={`text-sm ${runResult.total > 0 ? 'text-emerald-700' : 'text-amber-700'}`}>{runResult.message}</p>
        </div>
      )}

      {/* ── Sub-automations ── */}
      <div className="space-y-3">
        <h3 className="text-xs font-semibold text-slate-500 uppercase tracking-wide px-1">
          الأتمتة المضمّنة في الطيار
        </h3>
        {(Object.keys(AUTOPILOT_SUB_META) as (keyof Omit<AutopilotSettings, 'enabled'>)[]).map(subKey => (
          <SubAutomationCard
            key={subKey}
            subKey={subKey}
            config={ap[subKey]}
            onToggle={enabled => toggleSub(subKey, enabled)}
          />
        ))}
      </div>

      {/* ── Daily summary ── */}
      <DailySummaryPanel
        items={status?.daily_summary ?? []}
        lastRunAt={status?.last_run_at ?? null}
        onRunNow={handleRunNow}
        running={running}
      />

      {/* ── Scheduler info ── */}
      <div className="flex items-start gap-3 bg-slate-50 border border-slate-200 rounded-xl px-4 py-3">
        <Clock className="w-4 h-4 text-slate-400 shrink-0 mt-0.5" />
        <div className="text-xs text-slate-500 space-y-1">
          <p className="font-medium text-slate-600">الجدول الزمني للتشغيل</p>
          <p>الطيار يعمل كـ background job كل ساعة تلقائياً.</p>
          <p>انقر <strong>تشغيل الآن</strong> لاختبار الطيار يدوياً في أي وقت.</p>
        </div>
      </div>

    </div>
  )
}

// ── Tab: AI Sales Agent ───────────────────────────────────────────────────────

function AiSalesAgentTab() {
  const [settings, setSettings] = useState<AiSalesAgentSettings | null>(null)
  const [loading,  setLoading]  = useState(true)
  const [saving,   setSaving]   = useState(false)
  const [saved,    setSaved]    = useState(false)
  const [error,    setError]    = useState<string | null>(null)

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const res = await aiSalesApi.getSettings()
      setSettings(res.settings)
    } catch { /* non-fatal */ }
    finally { setLoading(false) }
  }, [])

  useEffect(() => { load() }, [load])

  const toggle = (key: keyof AiSalesAgentSettings, value: boolean) => {
    setSettings(s => s ? { ...s, [key]: value } : s)
  }

  const handleSave = async () => {
    if (!settings) return
    setSaving(true)
    setError(null)
    setSaved(false)
    try {
      const res = await aiSalesApi.saveSettings(settings)
      setSettings(res.settings)
      setSaved(true)
      setTimeout(() => setSaved(false), 3000)
    } catch {
      setError('تعذّر حفظ الإعدادات. حاول مجدداً.')
    } finally { setSaving(false) }
  }

  if (loading) {
    return (
      <div className="flex items-center justify-center py-20">
        <Loader2 className="w-6 h-6 animate-spin text-brand-500" />
        <span className="ms-3 text-sm text-slate-500">تحميل إعدادات وكيل المبيعات...</span>
      </div>
    )
  }

  const s = settings ?? {
    enable_ai_sales_agent: false, allow_product_recommendations: true,
    allow_order_creation: true, allow_address_collection: true,
    allow_payment_link_sending: true, allow_cod_confirmation_flow: true,
    allow_human_handoff: true, confidence_threshold: 0.55, handoff_phrases: [],
  }

  return (
    <div className="space-y-5">

      {/* ── Master enable card ── */}
      <div className={`card p-5 transition-all ${s.enable_ai_sales_agent ? 'ring-2 ring-brand-300 ring-offset-1' : ''}`}>
        <div className="flex items-start justify-between gap-4">
          <div className="flex items-start gap-4">
            <div className={`w-12 h-12 rounded-xl flex items-center justify-center shrink-0 ${
              s.enable_ai_sales_agent ? 'bg-brand-50' : 'bg-slate-100'
            }`}>
              <BrainCircuit className={`w-6 h-6 ${s.enable_ai_sales_agent ? 'text-brand-500' : 'text-slate-400'}`} />
            </div>
            <div>
              <div className="flex items-center gap-2 flex-wrap">
                <h2 className="text-base font-bold text-slate-900">وكيل المبيعات الذكي</h2>
                <span className={`text-xs font-medium px-2 py-0.5 rounded-full ${
                  s.enable_ai_sales_agent
                    ? 'bg-brand-100 text-brand-700'
                    : 'bg-slate-100 text-slate-500'
                }`}>
                  {s.enable_ai_sales_agent ? 'مُفعّل ✅' : 'معطّل'}
                </span>
              </div>
              <p className="text-xs text-slate-500 mt-1 max-w-lg">
                تحوّل نحلة إلى موظف مبيعات ذكي داخل محادثات واتساب — يتعرّف على نيّة العميل،
                يقترح المنتجات المناسبة، ينشئ الطلبات، ويرسل روابط الدفع، كل ذلك باستخدام بيانات متجرك الفعلية فقط.
              </p>
            </div>
          </div>
          <button
            onClick={() => toggle('enable_ai_sales_agent', !s.enable_ai_sales_agent)}
            className="shrink-0 mt-1"
          >
            {s.enable_ai_sales_agent
              ? <ToggleRight className="w-8 h-8 text-brand-500" />
              : <ToggleLeft  className="w-8 h-8 text-slate-300" />}
          </button>
        </div>
      </div>

      {/* ── Compliance / grounding notice ── */}
      <div className="flex items-start gap-3 bg-amber-50 border border-amber-200 rounded-xl px-4 py-3">
        <ShieldCheck className="w-4 h-4 text-amber-600 shrink-0 mt-0.5" />
        <p className="text-xs text-amber-800 leading-relaxed">
          <strong>بيانات حقيقية فقط:</strong> وكيل المبيعات لا يخترع منتجات أو أسعاراً.
          جميع ردوده مبنيّة على كتالوج منتجاتك الفعلي وبيانات المتجر المربوط.
          لا يتم إنشاء طلب دون تأكيد صريح من العميل.
        </p>
      </div>

      {/* ── Permissions grid ── */}
      <Section title="صلاحيات الوكيل" description="تحكّم بما يُسمح لوكيل المبيعات بتنفيذه في محادثات واتساب">
        <div className="space-y-1">
          {AI_SALES_PERMISSION_META.map(({ key, label, hint, icon }) => (
            <div key={key} className="flex items-start justify-between py-3 border-b border-slate-50 last:border-0">
              <div className="flex items-start gap-3">
                <span className="text-lg mt-0.5">{icon}</span>
                <div>
                  <p className="text-sm font-medium text-slate-800">{label}</p>
                  <p className="text-xs text-slate-400 mt-0.5">{hint}</p>
                </div>
              </div>
              <button
                onClick={() => toggle(key as keyof AiSalesAgentSettings, !(s[key as keyof AiSalesAgentSettings] as boolean))}
                className="ms-4 shrink-0"
              >
                {(s[key as keyof AiSalesAgentSettings] as boolean)
                  ? <ToggleRight className="w-6 h-6 text-brand-500" />
                  : <ToggleLeft  className="w-6 h-6 text-slate-300" />}
              </button>
            </div>
          ))}
        </div>
      </Section>

      {/* ── Intent coverage info ── */}
      <Section title="النوايا المدعومة" description="الوكيل يكشف هذه النوايا تلقائياً من رسائل واتساب">
        <div className="grid grid-cols-2 sm:grid-cols-3 gap-2">
          {[
            { e: '📦', l: 'استفسار عن منتج' },
            { e: '💰', l: 'استفسار عن السعر' },
            { e: '⭐', l: 'طلب توصية' },
            { e: '🚚', l: 'استفسار عن الشحن' },
            { e: '🏷️', l: 'استفسار عن العروض' },
            { e: '🛍️', l: 'طلب شراء منتج' },
            { e: '💳', l: 'الدفع الإلكتروني' },
            { e: '💵', l: 'الدفع عند الاستلام' },
            { e: '📍', l: 'تتبع الطلب' },
            { e: '👤', l: 'التحدث مع موظف' },
          ].map(({ e, l }) => (
            <div key={l} className="flex items-center gap-2 px-3 py-2 rounded-lg bg-slate-50 text-xs text-slate-700">
              <span>{e}</span>
              <span>{l}</span>
            </div>
          ))}
        </div>
      </Section>

      {/* ── Flow diagram ── */}
      <Section title="تدفق المحادثة إلى طلب" description="كيف يحوّل وكيل المبيعات رسالة واتساب إلى طلب مكتمل">
        <div className="flex flex-wrap items-center gap-2 text-xs text-slate-600">
          {[
            '💬 رسالة واتساب',
            '🧠 كشف النيّة',
            '📦 استعلام الكتالوج',
            '🗣️ رد مخصّص',
            '✅ تأكيد العميل',
            '📋 إنشاء الطلب',
          ].map((step, i, arr) => (
            <span key={step} className="flex items-center gap-2">
              <span className="bg-brand-50 text-brand-700 font-medium px-2.5 py-1 rounded-full">{step}</span>
              {i < arr.length - 1 && <span className="text-slate-300">←</span>}
            </span>
          ))}
        </div>
        <div className="mt-4 grid sm:grid-cols-2 gap-3">
          <div className="border border-emerald-100 bg-emerald-50 rounded-xl p-3">
            <p className="text-xs font-semibold text-emerald-700 mb-1">💵 تدفق الدفع عند الاستلام (COD)</p>
            <p className="text-xs text-emerald-600 leading-relaxed">
              إنشاء طلب ← وضع حالة "pending_confirmation" ← تشغيل تدفق تأكيد الطيار التلقائي ← تذكير ← إلغاء تلقائي عند عدم الرد
            </p>
          </div>
          <div className="border border-blue-100 bg-blue-50 rounded-xl p-3">
            <p className="text-xs font-semibold text-blue-700 mb-1">💳 تدفق الدفع الإلكتروني</p>
            <p className="text-xs text-blue-600 leading-relaxed">
              إنشاء طلب ← توليد رابط الدفع ← إرسال الرابط للعميل في المحادثة ← تسجيل حدث payment_link_sent
            </p>
          </div>
        </div>
      </Section>

      {/* ── Human handoff ── */}
      <Section title="التحويل لموظف بشري" description="متى وكيف يحوّل الوكيل المحادثة لموظف حقيقي">
        <div className="space-y-3 text-xs text-slate-600">
          <div className="flex items-start gap-3">
            <span className="w-5 h-5 rounded-full bg-slate-100 flex items-center justify-center text-slate-500 shrink-0 font-bold">١</span>
            <p><strong>طلب صريح:</strong> عندما يكتب العميل عبارات مثل "تكلم مع موظف" أو "بشري" أو "دعم"</p>
          </div>
          <div className="flex items-start gap-3">
            <span className="w-5 h-5 rounded-full bg-slate-100 flex items-center justify-center text-slate-500 shrink-0 font-bold">٢</span>
            <p><strong>ثقة منخفضة:</strong> عندما تكون نسبة ثقة الكشف عن النيّة أقل من {Math.round(s.confidence_threshold * 100)}%</p>
          </div>
          <div className="flex items-start gap-3">
            <span className="w-5 h-5 rounded-full bg-slate-100 flex items-center justify-center text-slate-500 shrink-0 font-bold">٣</span>
            <p><strong>التفعيل:</strong> يشترط تفعيل "السماح بالتحويل لموظف" في الصلاحيات أعلاه</p>
          </div>
        </div>
      </Section>

      {/* ── Logs link ── */}
      <div className="flex items-center gap-3 bg-slate-50 border border-slate-200 rounded-xl px-4 py-3">
        <BrainCircuit className="w-4 h-4 text-brand-500 shrink-0" />
        <p className="text-xs text-slate-600 flex-1">
          تتبّع كل محادثة وطلب ونيّة مكتشفة في سجل وكيل المبيعات.
        </p>
        <a href="/ai-sales-logs" className="text-xs text-brand-600 font-medium hover:underline flex items-center gap-1">
          عرض السجل
          <LinkOut className="w-3 h-3" />
        </a>
      </div>

      {/* ── Save bar ── */}
      <SaveBar onSave={handleSave} saving={saving} saved={saved} error={error} />
    </div>
  )
}

// ── Support Access Tab ────────────────────────────────────────────────────────

const TTL_OPTIONS = [
  { value: 1,  label: 'ساعة واحدة' },
  { value: 2,  label: 'ساعتان' },
  { value: 4,  label: '4 ساعات' },
  { value: 8,  label: '8 ساعات (الافتراضي)' },
  { value: 24, label: '24 ساعة' },
  { value: 48, label: '48 ساعة' },
]

function SupportAccessTab() {
  const [status, setStatus] = useState<{
    enabled: boolean; granted_at: string | null; expires_at: string | null; message: string
  } | null>(null)
  const [loading, setLoading]       = useState(true)
  const [saving, setSaving]         = useState(false)
  const [ttlHours, setTtlHours]     = useState(8)
  const [successMsg, setSuccessMsg] = useState<string | null>(null)
  const [errorMsg, setErrorMsg]     = useState<string | null>(null)

  const load = useCallback(async (silent = false) => {
    if (!silent) setLoading(true)
    try {
      const res = await fetch(`${API_BASE}/merchant/support-access`, {
        headers: { Authorization: `Bearer ${localStorage.getItem('nahla_token') ?? ''}` },
      })
      if (res.ok) setStatus(await res.json())
    } catch { /* ignore */ }
    finally { if (!silent) setLoading(false) }
  }, [])

  useEffect(() => {
    load()
    // Poll every 10s to catch changes from bell approval or admin entry
    const id = setInterval(() => load(true), 10_000)
    // Also listen for custom event dispatched by the bell when merchant approves
    const onApproved = () => load(true)
    window.addEventListener('nahla:support-access-changed', onApproved)
    return () => {
      clearInterval(id)
      window.removeEventListener('nahla:support-access-changed', onApproved)
    }
  }, [load])

  const toggle = async (enable: boolean) => {
    setSaving(true)
    setSuccessMsg(null)
    setErrorMsg(null)
    try {
      const endpoint = enable
        ? `${API_BASE}/merchant/support-access/enable`
        : `${API_BASE}/merchant/support-access/disable`
      const body = enable ? JSON.stringify({ ttl_hours: ttlHours }) : undefined
      const res = await fetch(endpoint, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          Authorization: `Bearer ${localStorage.getItem('nahla_token') ?? ''}`,
        },
        body,
      })
      const data = await res.json()
      if (!res.ok) throw new Error(data.detail ?? 'حدث خطأ')
      setSuccessMsg(data.message)
      await load()
    } catch (e: unknown) {
      setErrorMsg(e instanceof Error ? e.message : 'حدث خطأ غير متوقع')
    } finally {
      setSaving(false)
    }
  }

  const fmtDate = (iso: string | null) => {
    if (!iso) return '—'
    try {
      return new Intl.DateTimeFormat('ar-SA', {
        dateStyle: 'medium', timeStyle: 'short', timeZone: 'Asia/Riyadh',
      }).format(new Date(iso))
    } catch { return iso }
  }

  return (
    <div className="space-y-5">

      {/* ── ACTIVE ACCESS ALERT — prominent red banner ── */}
      {!loading && status?.enabled && (
        <div className="rounded-2xl border-2 border-red-400 bg-red-50 overflow-hidden">
          {/* Top bar */}
          <div className="flex items-center gap-3 bg-red-500 px-5 py-3">
            <span className="relative flex h-3 w-3">
              <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-white opacity-75" />
              <span className="relative inline-flex rounded-full h-3 w-3 bg-white" />
            </span>
            <p className="text-white font-bold text-sm flex-1">
              ⚠️ فريق الدعم الفني يمتلك صلاحية الدخول إلى لوحتك الآن
            </p>
          </div>
          {/* Details */}
          <div className="px-5 py-4 space-y-3">
            <div className="grid grid-cols-2 gap-3 text-xs">
              <div className="bg-white rounded-lg p-3 border border-red-100">
                <p className="text-slate-400 mb-0.5">مُفعّل منذ</p>
                <p className="font-semibold text-slate-800">{fmtDate(status.granted_at)}</p>
              </div>
              <div className="bg-white rounded-lg p-3 border border-red-100">
                <p className="text-slate-400 mb-0.5">ينتهي تلقائياً</p>
                <p className="font-semibold text-red-700">{fmtDate(status.expires_at)}</p>
              </div>
            </div>
            <p className="text-xs text-red-700">
              إذا لم تكن أنت من طلب هذا الدعم أو انتهيت من المشكلة، ألغِ الوصول فوراً.
            </p>
            <button
              onClick={() => toggle(false)}
              disabled={saving}
              className="w-full flex items-center justify-center gap-2 py-3 rounded-xl bg-red-600 hover:bg-red-700 text-white font-bold text-sm transition-colors disabled:opacity-60"
            >
              {saving
                ? <Loader2 className="w-4 h-4 animate-spin" />
                : <ShieldOff className="w-4 h-4" />}
              إلغاء وصول الدعم الفني فوراً
            </button>
            {successMsg && (
              <div className="flex items-center gap-2 p-2.5 bg-green-50 border border-green-200 rounded-lg text-xs text-green-700">
                <CheckCircle className="w-4 h-4 shrink-0" /> {successMsg}
              </div>
            )}
          </div>
        </div>
      )}

      {/* ── Main card ── */}
      <div className="card p-5">
        <div className="flex items-start gap-3 mb-4">
          <div className={`w-9 h-9 rounded-lg flex items-center justify-center shrink-0 ${
            status?.enabled ? 'bg-red-100' : 'bg-green-100'
          }`}>
            {status?.enabled
              ? <ShieldOff className="w-5 h-5 text-red-600" />
              : <ShieldCheck className="w-5 h-5 text-green-600" />}
          </div>
          <div>
            <h3 className="text-sm font-semibold text-slate-900">وصول الدعم الفني</h3>
            <p className="text-xs text-slate-500 mt-0.5">
              امنح فريق نحلة صلاحية مؤقتة للدخول إلى لوحتك لمساعدتك في حل مشكلة.
              لا يمكن لأي أحد — بما في ذلك فريق الدعم — الدخول بدون موافقتك الصريحة.
            </p>
          </div>
        </div>

        {loading ? (
          <div className="flex items-center gap-2 py-4 text-slate-400 text-sm">
            <Loader2 className="w-4 h-4 animate-spin" /> جارٍ التحميل...
          </div>
        ) : (
          <>
            {/* Status indicator */}
            <div className={`flex items-center gap-2 p-3 rounded-lg mb-4 ${
              status?.enabled
                ? 'bg-red-50 border border-red-200'
                : 'bg-green-50 border border-green-200'
            }`}>
              {status?.enabled ? (
                <>
                  <span className="relative flex h-2.5 w-2.5 shrink-0">
                    <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-red-400 opacity-75" />
                    <span className="relative inline-flex rounded-full h-2.5 w-2.5 bg-red-500" />
                  </span>
                  <p className="text-xs font-bold text-red-700">
                    الوصول مفعّل — فريق الدعم يستطيع الدخول الآن
                  </p>
                </>
              ) : (
                <>
                  <ShieldCheck className="w-4 h-4 text-green-600 shrink-0" />
                  <p className="text-xs font-semibold text-green-700">
                    لوحتك محمية — لا أحد يستطيع الدخول
                  </p>
                </>
              )}
            </div>

            {/* Enable form — only when NOT active */}
            {!status?.enabled && (
              <div className="space-y-3">
                <label className="block text-xs font-medium text-slate-700">
                  مدة الوصول المؤقت
                </label>
                <div className="grid grid-cols-3 gap-2">
                  {TTL_OPTIONS.map(opt => (
                    <button
                      key={opt.value}
                      onClick={() => setTtlHours(opt.value)}
                      className={`py-2 px-3 text-xs rounded-lg border transition-colors ${
                        ttlHours === opt.value
                          ? 'bg-brand-500 text-white border-brand-500'
                          : 'border-slate-200 text-slate-600 hover:bg-slate-50'
                      }`}
                    >
                      {opt.label}
                    </button>
                  ))}
                </div>
                <button
                  onClick={() => toggle(true)}
                  disabled={saving}
                  className="btn-primary w-full flex items-center justify-center gap-2"
                >
                  {saving ? <Loader2 className="w-4 h-4 animate-spin" /> : <ShieldCheck className="w-4 h-4" />}
                  السماح للدعم الفني بالوصول لمدة {TTL_OPTIONS.find(o => o.value === ttlHours)?.label}
                </button>
              </div>
            )}

            {/* Messages */}
            {successMsg && (
              <div className="flex items-center gap-2 mt-3 p-2.5 bg-green-50 border border-green-200 rounded-lg text-xs text-green-700">
                <CheckCircle className="w-4 h-4 shrink-0" /> {successMsg}
              </div>
            )}
            {errorMsg && (
              <div className="flex items-center gap-2 mt-3 p-2.5 bg-red-50 border border-red-200 rounded-lg text-xs text-red-600">
                <AlertCircle className="w-4 h-4 shrink-0" /> {errorMsg}
              </div>
            )}
          </>
        )}
      </div>

      {/* Info card */}
      <div className="card p-5 bg-slate-50">
        <h4 className="text-xs font-semibold text-slate-700 mb-2">كيف يعمل هذا النظام؟</h4>
        <ul className="space-y-1.5 text-xs text-slate-500 list-none">
          <li className="flex gap-2"><span className="text-brand-500 font-bold">1.</span> أنت من يقرر متى يُسمح للدعم الفني بالدخول</li>
          <li className="flex gap-2"><span className="text-brand-500 font-bold">2.</span> الوصول مؤقت وينتهي تلقائياً بعد المدة التي تحددها</li>
          <li className="flex gap-2"><span className="text-brand-500 font-bold">3.</span> يمكنك إلغاء الوصول في أي لحظة قبل انتهاء المدة</li>
          <li className="flex gap-2"><span className="text-brand-500 font-bold">4.</span> كل دخول للدعم الفني يُسجَّل في سجلات المنصة</li>
          <li className="flex gap-2"><span className="text-brand-500 font-bold">5.</span> بدون موافقتك، لا يستطيع أحد — بما في ذلك المالك — رؤية لوحتك</li>
        </ul>
      </div>

      {/* Pending access requests from admin */}
      <AccessRequestsPanel onApproved={load} />
    </div>
  )
}

interface AccessRequest {
  id: string
  requested_by: string
  requested_at: string
  store_name: string
}

const TTL_OPTS_SMALL = [
  { value: 1, label: 'ساعة'   },
  { value: 2, label: 'ساعتان' },
  { value: 4, label: '4 ساعات'},
]

function AccessRequestsPanel({ onApproved }: { onApproved?: () => void }) {
  const [requests, setRequests]   = useState<AccessRequest[]>([])
  const [loading, setLoading]     = useState(true)
  const [responding, setResponding] = useState<string | null>(null)
  const [ttl, setTtl]             = useState(4)

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const res = await fetch(`${API_BASE}/merchant/access-requests`, {
        headers: { Authorization: `Bearer ${localStorage.getItem('nahla_token') ?? ''}` },
      })
      if (res.ok) {
        const d = await res.json()
        setRequests(d.requests ?? [])
      }
    } catch { /* ignore */ }
    finally { setLoading(false) }
  }, [])

  useEffect(() => { load() }, [load])

  const respond = async (reqId: string, approve: boolean) => {
    setResponding(reqId)
    try {
      const res = await fetch(`${API_BASE}/merchant/access-requests/${reqId}/respond`, {
        method:  'POST',
        headers: {
          'Content-Type': 'application/json',
          Authorization: `Bearer ${localStorage.getItem('nahla_token') ?? ''}`,
        },
        body: JSON.stringify({ approve, ttl_hours: ttl }),
      })
      if (res.ok) {
        await load()
        if (approve) {
          // Notify SupportAccessTab to refresh immediately
          window.dispatchEvent(new Event('nahla:support-access-changed'))
          if (onApproved) onApproved()
        }
      }
    } catch { /* ignore */ }
    finally { setResponding(null) }
  }

  if (loading || requests.length === 0) return null

  return (
    <div className="card p-5 border-amber-200 bg-amber-50">
      <div className="flex items-center gap-2 mb-3">
        <Bell className="w-4 h-4 text-amber-600" />
        <h4 className="text-sm font-semibold text-amber-800">
          طلبات وصول معلّقة ({requests.length})
        </h4>
      </div>
      <div className="space-y-3">
        {requests.map(r => (
          <div key={r.id} className="bg-white rounded-xl p-4 border border-amber-100 space-y-3">
            <div>
              <p className="text-sm font-medium text-slate-800">
                فريق نحلة يطلب الوصول إلى لوحتك
              </p>
              <p className="text-xs text-slate-500 mt-0.5">
                الطلب من: <span className="font-medium">{r.requested_by}</span>
                {' · '}
                {new Date(r.requested_at).toLocaleString('ar-SA')}
              </p>
            </div>

            {/* TTL selector for approval */}
            <div className="flex items-center gap-2 flex-wrap">
              <span className="text-xs text-slate-500">مدة الوصول عند الموافقة:</span>
              {TTL_OPTS_SMALL.map(o => (
                <button
                  key={o.value}
                  onClick={() => setTtl(o.value)}
                  className={`px-2.5 py-1 text-xs rounded-lg border transition-colors ${
                    ttl === o.value
                      ? 'bg-amber-500 text-white border-amber-500'
                      : 'border-slate-200 text-slate-600 hover:bg-slate-50'
                  }`}
                >
                  {o.label}
                </button>
              ))}
            </div>

            <div className="flex gap-2">
              <button
                onClick={() => respond(r.id, true)}
                disabled={responding === r.id}
                className="flex-1 btn-primary flex items-center justify-center gap-1.5 text-xs py-2"
              >
                {responding === r.id
                  ? <Loader2 className="w-3.5 h-3.5 animate-spin" />
                  : <ShieldCheck className="w-3.5 h-3.5" />}
                موافقة
              </button>
              <button
                onClick={() => respond(r.id, false)}
                disabled={responding === r.id}
                className="flex-1 btn-secondary flex items-center justify-center gap-1.5 text-xs py-2 border-red-200 text-red-600 hover:bg-red-50"
              >
                <ShieldOff className="w-3.5 h-3.5" />
                رفض
              </button>
            </div>
          </div>
        ))}
      </div>
    </div>
  )
}

// ── Tab: WhatsApp Widget ─────────────────────────────────────────────────────

const NAHLA_CDN_LOGO = 'https://cdn.salla.sa/XVEDq/b1ec4359-6895-49dc-80e7-06fd33b75df8-1000x666.66666666667-xMM28RbT68xVWoSgEtzBgpW1w4cDN7sEQAhQmLwD.jpg'

function generateWidgetCode(cfg: {
  phone: string
  message: string
  logoUrl: string
  position: 'left' | 'right'
  scrollThreshold: number
}): string {
  const logo   = cfg.logoUrl || NAHLA_CDN_LOGO
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
    if (window.scrollY > ${cfg.scrollThreshold} || document.body.scrollHeight <= window.innerHeight + 300) {
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

interface WidgetConfig {
  enabled: boolean
  phone: string
  message: string
  logo_url: string
  position: 'left' | 'right'
  scroll_threshold: number
}

function WidgetTab() {
  const [cfg, setCfg] = useState<WidgetConfig>({
    enabled: false, phone: '', message: 'السلام عليكم، أبغى الاستفسار',
    logo_url: '', position: 'left', scroll_threshold: 250,
  })
  const [loading, setLoading]   = useState(true)
  const [saving, setSaving]     = useState(false)
  const [saved, setSaved]       = useState(false)
  const [copied, setCopied]     = useState(false)
  const [saveErr, setSaveErr]   = useState<string | null>(null)

  useEffect(() => {
    apiCall<WidgetConfig>('/settings/widget')
      .then(d => setCfg({ ...d, position: (d.position === 'right' ? 'right' : 'left') }))
      .catch(() => {})
      .finally(() => setLoading(false))
  }, []) // eslint-disable-line react-hooks/exhaustive-deps

  const patch = (partial: Partial<WidgetConfig>) => setCfg(prev => ({ ...prev, ...partial }))

  const handleSave = async () => {
    setSaving(true); setSaveErr(null)
    try {
      const saved_ = await apiCall<WidgetConfig>('/settings/widget', {
        method: 'PUT',
        body: JSON.stringify(cfg),
      })
      setCfg({ ...saved_, position: saved_.position === 'right' ? 'right' : 'left' })
      setSaved(true); setTimeout(() => setSaved(false), 3000)
    } catch { setSaveErr('فشل الحفظ — حاول مرة أخرى') }
    finally { setSaving(false) }
  }

  const code = generateWidgetCode({
    phone: cfg.phone, message: cfg.message,
    logoUrl: cfg.logo_url, position: cfg.position,
    scrollThreshold: cfg.scroll_threshold,
  })

  const copyCode = () => {
    navigator.clipboard.writeText(code)
    setCopied(true); setTimeout(() => setCopied(false), 2500)
  }

  if (loading) return (
    <div className="flex items-center justify-center py-16 gap-2 text-slate-400 text-sm">
      <Loader2 className="w-4 h-4 animate-spin" /> جاري التحميل...
    </div>
  )

  return (
    <div className="space-y-5">

      {/* ── Enable / Disable ── */}
      <Section title="ويدجت واتساب" description="أداة زر واتساب العائمة تظهر في متجرك لتسهيل تواصل العملاء">
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
      </Section>

      {/* ── Config fields ── */}
      <Section title="إعدادات الويدجت">
        <div className="space-y-4">

          {/* Phone */}
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

          {/* Message */}
          <div>
            <label className="label">رسالة الترحيب الافتراضية</label>
            <input
              className="input"
              value={cfg.message}
              onChange={e => patch({ message: e.target.value })}
            />
          </div>

          {/* Logo */}
          <div>
            <label className="label">رابط الشعار (اختياري)</label>
            <input
              className="input" dir="ltr"
              placeholder="https://cdn.example.com/logo.png — اتركه فارغاً لاستخدام شعار نحلة"
              value={cfg.logo_url}
              onChange={e => patch({ logo_url: e.target.value })}
            />
            <p className="text-xs text-slate-400 mt-1">اتركه فارغاً لاستخدام شعار نحلة الافتراضي 🐝</p>
          </div>

          {/* Position + Scroll threshold */}
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
      </Section>

      {/* ── Save button ── */}
      <div className="flex items-center gap-3">
        <button
          onClick={handleSave}
          disabled={saving || !cfg.phone}
          className="btn-primary disabled:opacity-50"
        >
          {saving ? <Loader2 className="w-4 h-4 animate-spin" /> : <Save className="w-4 h-4" />}
          حفظ الإعدادات
        </button>
        {saved  && <span className="text-sm text-emerald-600 flex items-center gap-1"><CheckCircle className="w-4 h-4" /> تم الحفظ</span>}
        {saveErr && <span className="text-sm text-red-500">{saveErr}</span>}
      </div>

      {/* ── Generated code ── */}
      <Section
        title="كود التضمين"
        description="انسخ الكود التالي وضعه في حقل الجافاسكريبت المخصص في متجرك (سلة / زد / غيرها)"
      >
        {!cfg.phone ? (
          <div className="flex items-center gap-2 p-4 bg-amber-50 border border-amber-200 rounded-xl text-sm text-amber-700">
            <AlertCircle className="w-4 h-4 shrink-0" />
            أدخل رقم واتساب أولاً لتوليد الكود
          </div>
        ) : (
          <div className="space-y-3">
            <div className="relative">
              <textarea
                readOnly
                dir="ltr"
                rows={10}
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
      </Section>
    </div>
  )
}


// ── Main Settings page ───────────────────────────────────────────────────────

export default function Settings() {
  const { t } = useLanguage()
  const [activeTab, setActiveTab] = useState<TabId>('whatsapp')
  const [settings, setSettings] = useState<AllSettings | null>(null)
  const [loading, setLoading] = useState(true)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)
  const [savedTab, setSavedTab] = useState<TabId | null>(null)
  const [saveError, setSaveError] = useState<string | null>(null)

  const TABS = TAB_IDS.map(id => ({
    id,
    label: t(tr => tr.settings.tabs[id as keyof typeof tr.settings.tabs]),
    icon: TAB_ICONS[id],
  }))

  useEffect(() => {
    settingsApi.getAll()
      .then(setSettings)
      .catch(() => setLoadError(t(tr => tr.meta.code) === 'ar'
        ? 'تعذّر تحميل الإعدادات. تأكد من تشغيل الخادم الخلفي.'
        : 'Failed to load settings. Make sure the backend is running.'))
      .finally(() => setLoading(false))
  }, [])

  const patchWhatsApp = (patch: Partial<WhatsAppSettings>) =>
    setSettings(s => s ? { ...s, whatsapp: { ...s.whatsapp, ...patch } } : s)

  const patchAI = (patch: Partial<AISettings>) =>
    setSettings(s => s ? { ...s, ai: { ...s.ai, ...patch } } : s)

  const patchStore = (patch: Partial<StoreSettings>) =>
    setSettings(s => s ? { ...s, store: { ...s.store, ...patch } } : s)

  const patchNotifications = (patch: Partial<NotificationSettings>) =>
    setSettings(s => s ? { ...s, notifications: { ...s.notifications, ...patch } } : s)

  const handleSave = async (tab: TabId) => {
    if (!settings) return
    setSaving(true)
    setSaveError(null)
    setSavedTab(null)
    try {
      const updated = await settingsApi.update({
        whatsapp:      tab === 'whatsapp'       ? settings.whatsapp      : undefined,
        ai:            tab === 'ai'             ? settings.ai            : undefined,
        store:         tab === 'store'          ? settings.store         : undefined,
        notifications: tab === 'notifications'  ? settings.notifications : undefined,
      })
      setSettings(updated)
      setSavedTab(tab)
      setTimeout(() => setSavedTab(null), 3000)
    } catch {
      setSaveError('فشل الحفظ – يرجى المحاولة مرة أخرى')
    } finally {
      setSaving(false)
    }
  }

  if (loading) {
    return (
      <div className="flex items-center justify-center py-20">
        <Loader2 className="w-6 h-6 animate-spin text-brand-500" />
        <span className="ms-3 text-sm text-slate-500">تحميل الإعدادات...</span>
      </div>
    )
  }

  if (loadError || !settings) {
    return (
      <div className="card p-8 max-w-md flex flex-col items-center gap-3 text-center">
        <AlertCircle className="w-8 h-8 text-red-400" />
        <p className="text-sm text-slate-700">{loadError ?? 'حدث خطأ غير متوقع'}</p>
        <button className="btn-secondary text-sm" onClick={() => window.location.reload()}>
          <RefreshCw className="w-4 h-4" /> إعادة المحاولة
        </button>
      </div>
    )
  }

  return (
    <div className="space-y-5">
      {/* Tab bar — full-width border-bottom like other pages */}
      <div className="border-b border-slate-200 -mx-3 px-3 md:-mx-6 md:px-6">
        <div className="flex gap-1 overflow-x-auto">
          {TABS.map(({ id, label, icon: Icon }) => (
            <button
              key={id}
              onClick={() => setActiveTab(id)}
              className={`flex items-center gap-2 px-4 py-2.5 text-sm font-medium whitespace-nowrap transition-colors border-b-2 -mb-px ${
                activeTab === id
                  ? 'border-brand-500 text-brand-600'
                  : 'border-transparent text-slate-500 hover:text-slate-700 hover:border-slate-300'
              }`}
            >
              <Icon className="w-4 h-4 shrink-0" />
              {label}
            </button>
          ))}
        </div>
      </div>

      {/* Tab content */}
      {activeTab === 'whatsapp' && (
        <WhatsAppTab
          data={settings.whatsapp}
          onChange={patchWhatsApp}
          onSave={() => handleSave('whatsapp')}
          saving={saving}
          saved={savedTab === 'whatsapp'}
          saveError={activeTab === 'whatsapp' ? saveError : null}
        />
      )}
      {activeTab === 'ai' && (
        <AITab
          data={settings.ai}
          onChange={patchAI}
          onSave={() => handleSave('ai')}
          saving={saving}
          saved={savedTab === 'ai'}
          saveError={activeTab === 'ai' ? saveError : null}
        />
      )}
      {activeTab === 'automation' && <AutopilotTab />}
      {activeTab === 'ai_sales' && <AiSalesAgentTab />}
      {activeTab === 'store' && (
        <StoreTab
          data={settings.store}
          onChange={patchStore}
          onSave={() => handleSave('store')}
          saving={saving}
          saved={savedTab === 'store'}
          saveError={activeTab === 'store' ? saveError : null}
        />
      )}
      {activeTab === 'team' && <TeamTab />}
      {activeTab === 'notifications' && (
        <NotificationsTab
          data={settings.notifications}
          onChange={patchNotifications}
          onSave={() => handleSave('notifications')}
          saving={saving}
          saved={savedTab === 'notifications'}
          saveError={activeTab === 'notifications' ? saveError : null}
        />
      )}
      {activeTab === 'security' && <SupportAccessTab />}
      {activeTab === 'widget'   && <WidgetTab />}
    </div>
  )
}
