import { useState, useEffect, useCallback } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { isPlatformOwner } from '../auth'
import {
  Save, Bot, Store, Users, Bell, MessageSquare,
  CheckCircle, AlertCircle, Loader2, Copy,
  Eye, EyeOff, RefreshCw, UserPlus, Shield, ShieldOff, ToggleLeft, ToggleRight,
  Sparkles, BrainCircuit, ShieldCheck, Code2, ChevronRight,
  HeadphonesIcon, Send, Clock, X, ChevronDown, History,
  AlertTriangle, Wifi, Zap, Package,
} from 'lucide-react'
import { useLanguage } from '../i18n/context'
import { settingsApi, type AllSettings, type NotificationSettings } from '../api/settings'
import { API_BASE } from '../api/client'

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

// ── Quick Access — links to dedicated pages ──────────────────────────────────

const QUICK_LINKS = [
  {
    icon: MessageSquare,
    label: 'واتساب للأعمال',
    desc: 'ربط الرقم، الردود التلقائية، إعدادات الأزرار',
    to: '/whatsapp-connect',
    color: 'text-emerald-600 bg-emerald-50 border-emerald-200',
    iconColor: 'text-emerald-600',
  },
  {
    icon: Bot,
    label: 'الذكاء الاصطناعي',
    desc: 'شخصية نحلة، التعليمات، نبرة الرد، اللغة',
    to: '/intelligence',
    color: 'text-brand-600 bg-brand-50 border-brand-200',
    iconColor: 'text-brand-600',
  },
  {
    icon: BrainCircuit,
    label: 'وكيل المبيعات الذكي',
    desc: 'صلاحيات الوكيل، إنشاء الطلبات، روابط الدفع',
    to: '/ai-sales-logs',
    color: 'text-purple-600 bg-purple-50 border-purple-200',
    iconColor: 'text-purple-600',
  },
  {
    icon: Sparkles,
    label: 'الطيار الآلي',
    desc: 'أتمتة الطلبات، السلات المتروكة، استرجاع العملاء',
    to: '/smart-automations',
    color: 'text-amber-600 bg-amber-50 border-amber-200',
    iconColor: 'text-amber-600',
  },
  {
    icon: Store,
    label: 'المتجر والتكاملات',
    desc: 'ربط سلة / زد / شوبيفاي، بيانات المتجر',
    to: '/store-integration',
    color: 'text-sky-600 bg-sky-50 border-sky-200',
    iconColor: 'text-sky-600',
  },
  {
    icon: Code2,
    label: 'أدوات زيادة المبيعات',
    desc: 'ويدجت واتساب العائم، ويدجتات المتجر التحويلية',
    to: '/widgets',
    color: 'text-rose-600 bg-rose-50 border-rose-200',
    iconColor: 'text-rose-600',
  },
]

function QuickAccess() {
  return (
    <div className="card overflow-hidden">
      <div className="px-5 py-4 border-b border-slate-100 bg-slate-50">
        <h2 className="text-sm font-semibold text-slate-900">إعدادات الميزات الرئيسية</h2>
        <p className="text-xs text-slate-400 mt-0.5">
          كل ميزة لها صفحتها المخصصة — انتقل إليها مباشرة من هنا
        </p>
      </div>
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-px bg-slate-100">
        {QUICK_LINKS.map(({ icon: Icon, label, desc, to, iconColor }) => (
          <Link
            key={to}
            to={to}
            className="flex items-start gap-3 p-4 bg-white hover:bg-slate-50 transition-colors group"
          >
            <div className={`w-9 h-9 rounded-xl flex items-center justify-center shrink-0 bg-slate-100 ${iconColor}`}>
              <Icon className="w-4 h-4" />
            </div>
            <div className="flex-1 min-w-0">
              <p className="text-sm font-semibold text-slate-800 group-hover:text-brand-600 transition-colors">{label}</p>
              <p className="text-xs text-slate-400 mt-0.5 leading-relaxed">{desc}</p>
            </div>
            <ChevronRight className="w-4 h-4 text-slate-300 group-hover:text-brand-400 shrink-0 mt-1 transition-colors" />
          </Link>
        ))}
      </div>
    </div>
  )
}

// ── Tab IDs ──────────────────────────────────────────────────────────────────

const TAB_IDS = ['team', 'notifications', 'support', 'security', 'system'] as const
type TabId = typeof TAB_IDS[number]

const TAB_ICONS: Record<TabId, React.ComponentType<{ className?: string }>> = {
  team:          Users,
  notifications: Bell,
  support:       HeadphonesIcon,
  security:      ShieldCheck,
  system:        RefreshCw,
}

const TAB_LABELS: Record<TabId, string> = {
  team:          'الفريق',
  notifications: 'الإشعارات',
  support:       'الدعم والصلاحيات',
  security:      'الأمان',
  system:        'النظام',
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

      {/* Notification Log Panel */}
      <NotificationLogPanel />
    </div>
  )
}

// ── Notification Log Panel ────────────────────────────────────────────────────

interface NotifLogEntry {
  id: number
  event: string
  event_ar: string
  type: string
  status: string
  status_ar: string
  reason_ar: string
  details: { phone?: string; preview?: string }
  created_at: string
}

function NotificationLogPanel() {
  const [logs, setLogs]       = useState<NotifLogEntry[]>([])
  const [summary, setSummary] = useState<{ sent: number; skipped: number; total: number } | null>(null)
  const [loading, setLoading] = useState(true)
  const [open, setOpen]       = useState(false)

  useEffect(() => {
    const load = async () => {
      setLoading(true)
      try {
        const token = localStorage.getItem('nahla_token') ?? ''
        const [logsRes, sumRes] = await Promise.all([
          fetch(`${API_BASE}/merchant/notification-logs?limit=30&days=7`, {
            headers: { Authorization: `Bearer ${token}` },
          }),
          fetch(`${API_BASE}/merchant/notification-logs/summary?days=7`, {
            headers: { Authorization: `Bearer ${token}` },
          }),
        ])
        if (logsRes.ok) setLogs((await logsRes.json()).logs ?? [])
        if (sumRes.ok)  setSummary(await sumRes.json())
      } catch { /* ignore */ }
      finally { setLoading(false) }
    }
    load()
  }, [])

  const fmt = (iso: string) => {
    try {
      return new Intl.DateTimeFormat('ar-SA', {
        dateStyle: 'short', timeStyle: 'short', timeZone: 'Asia/Riyadh',
      }).format(new Date(iso))
    } catch { return iso }
  }

  return (
    <div className="card">
      <div className="px-5 py-4 border-b border-slate-100">
        <div className="flex items-center justify-between">
          <div>
            <h2 className="text-sm font-semibold text-slate-900">سجل إشعارات الإيميل</h2>
            <p className="text-xs text-slate-400 mt-0.5">آخر 7 أيام — متى أُرسل الإيميل ومتى لم يُرسل ولماذا</p>
          </div>
          {!loading && summary && (
            <div className="flex items-center gap-3 text-xs">
              <span className="flex items-center gap-1 text-green-600 font-medium">
                <CheckCircle className="w-3.5 h-3.5" />
                {summary.sent} أُرسل
              </span>
              <span className="flex items-center gap-1 text-slate-400 font-medium">
                <AlertCircle className="w-3.5 h-3.5" />
                {summary.skipped} تجاوز
              </span>
            </div>
          )}
        </div>
      </div>

      <div className="p-5">
        {loading ? (
          <div className="flex items-center gap-2 text-slate-400 text-sm">
            <Loader2 className="w-4 h-4 animate-spin" /> جارٍ التحميل...
          </div>
        ) : logs.length === 0 ? (
          <p className="text-sm text-slate-400 text-center py-4">
            لا يوجد سجل إشعارات بعد — سيظهر هنا عند أول رسالة واتساب
          </p>
        ) : (
          <div className="space-y-2">
            {/* Show first 5, toggle rest */}
            {(open ? logs : logs.slice(0, 5)).map(log => (
              <div
                key={log.id}
                className={`flex items-start gap-3 p-3 rounded-xl text-xs ${
                  log.status === 'sent'
                    ? 'bg-green-50 border border-green-100'
                    : 'bg-slate-50 border border-slate-100'
                }`}
              >
                <div className="shrink-0 mt-0.5">
                  {log.status === 'sent'
                    ? <CheckCircle className="w-3.5 h-3.5 text-green-500" />
                    : <AlertCircle className="w-3.5 h-3.5 text-slate-400" />
                  }
                </div>
                <div className="flex-1 min-w-0">
                  <div className="flex items-center justify-between gap-2 mb-0.5">
                    <span className={`font-semibold ${log.status === 'sent' ? 'text-green-700' : 'text-slate-600'}`}>
                      {log.status_ar} — {log.event_ar}
                    </span>
                    <span className="text-slate-400 shrink-0">{fmt(log.created_at)}</span>
                  </div>
                  {log.status === 'skipped' && log.reason_ar && (
                    <p className="text-slate-500">
                      السبب: {log.reason_ar}
                    </p>
                  )}
                  {log.details?.phone && (
                    <p className="text-slate-400 mt-0.5 font-mono" dir="ltr">{log.details.phone}</p>
                  )}
                  {log.details?.preview && (
                    <p className="text-slate-400 mt-0.5 truncate" style={{ maxWidth: '24ch' }}>
                      "{log.details.preview}"
                    </p>
                  )}
                </div>
              </div>
            ))}

            {logs.length > 5 && (
              <button
                onClick={() => setOpen(o => !o)}
                className="w-full text-xs text-slate-400 hover:text-slate-600 py-2 flex items-center justify-center gap-1"
              >
                <ChevronDown className={`w-3.5 h-3.5 transition-transform ${open ? 'rotate-180' : ''}`} />
                {open ? 'عرض أقل' : `عرض ${logs.length - 5} إشعار إضافي`}
              </button>
            )}
          </div>
        )}
      </div>
    </div>
  )
}

// ── Tab: Support Access (Security) ───────────────────────────────────────────

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
    const id = setInterval(() => load(true), 10_000)
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
      {!loading && status?.enabled && (
        <div className="rounded-2xl border-2 border-red-400 bg-red-50 overflow-hidden">
          <div className="flex items-center gap-3 bg-red-500 px-5 py-3">
            <span className="relative flex h-3 w-3">
              <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-white opacity-75" />
              <span className="relative inline-flex rounded-full h-3 w-3 bg-white" />
            </span>
            <p className="text-white font-bold text-sm flex-1">
              ⚠️ فريق الدعم الفني يمتلك صلاحية الدخول إلى لوحتك الآن
            </p>
          </div>
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

      <AccessRequestsPanel onApproved={load} />
    </div>
  )
}

// ── Tab: Support & Permissions ────────────────────────────────────────────────
// (Option B: standalone tab with help request + access management + history)

const PROBLEM_TYPES = [
  { value: 'whatsapp',  label: 'واتساب',        icon: '📱' },
  { value: 'orders',    label: 'الطلبات',        icon: '📦' },
  { value: 'autopilot', label: 'الطيار الآلي',   icon: '🤖' },
  { value: 'campaigns', label: 'الحملات',         icon: '📣' },
  { value: 'templates', label: 'القوالب',         icon: '📋' },
  { value: 'general',   label: 'مشكلة عامة',     icon: '💬' },
]

const TTL_OPTS_HELP = [
  { value: 4,  label: '4 ساعات (افتراضي)' },
  { value: 8,  label: '8 ساعات' },
  { value: 24, label: '24 ساعة' },
]

// ── Help Request Modal ─────────────────────────────────────────────────────────

function HelpRequestModal({ onClose, onSent }: { onClose: () => void; onSent: () => void }) {
  const [problemType, setProblemType] = useState('general')
  const [description, setDescription] = useState('')
  const [ttlHours, setTtlHours]       = useState(4)
  const [busy, setBusy]               = useState(false)
  const [error, setError]             = useState('')

  const submit = async () => {
    if (!description.trim() || description.trim().length < 10) {
      setError('يرجى وصف المشكلة بإيجاز (10 أحرف على الأقل)'); return
    }
    setBusy(true); setError('')
    try {
      const res = await fetch(`${API_BASE}/merchant/request-help`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          Authorization: `Bearer ${localStorage.getItem('nahla_token') ?? ''}`,
        },
        body: JSON.stringify({ problem_type: problemType, description: description.trim(), ttl_hours: ttlHours }),
      })
      const data = await res.json()
      if (res.status === 409) { setError(data.detail || 'يوجد طلب معلّق'); return }
      if (!res.ok) { setError(data.detail || 'حدث خطأ'); return }
      onSent()
    } catch { setError('خطأ في الاتصال') }
    finally { setBusy(false) }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center" dir="rtl">
      <div className="absolute inset-0 bg-black/50" onClick={onClose} />
      <div className="relative bg-white rounded-2xl shadow-2xl w-full max-w-lg mx-4 p-6 space-y-5">
        <div className="flex items-start justify-between">
          <div className="flex items-center gap-3">
            <div className="w-10 h-10 bg-amber-100 rounded-xl flex items-center justify-center shrink-0">
              <HeadphonesIcon className="w-5 h-5 text-amber-600" />
            </div>
            <div>
              <h2 className="text-base font-black text-slate-800">طلب مساعدة من نحلة</h2>
              <p className="text-xs text-slate-400">سيتواصل فريق الدعم معك قريباً داخل المنصة</p>
            </div>
          </div>
          <button onClick={onClose} className="text-slate-400 hover:text-slate-600">
            <X className="w-5 h-5" />
          </button>
        </div>

        {/* Problem type */}
        <div className="space-y-2">
          <label className="text-xs font-semibold text-slate-700">نوع المشكلة</label>
          <div className="grid grid-cols-3 gap-2">
            {PROBLEM_TYPES.map(pt => (
              <button
                key={pt.value}
                onClick={() => setProblemType(pt.value)}
                className={`flex flex-col items-center gap-1 py-2.5 px-2 rounded-xl border text-xs font-medium transition ${
                  problemType === pt.value
                    ? 'bg-amber-500 text-white border-amber-500'
                    : 'border-slate-200 text-slate-600 hover:border-amber-300 hover:bg-amber-50'
                }`}
              >
                <span className="text-base">{pt.icon}</span>
                <span>{pt.label}</span>
              </button>
            ))}
          </div>
        </div>

        {/* Description */}
        <div className="space-y-1.5">
          <label className="text-xs font-semibold text-slate-700">
            اشرح مشكلتك <span className="text-red-500">*</span>
          </label>
          <textarea
            value={description}
            onChange={e => setDescription(e.target.value)}
            placeholder="مثال: واتساب لا يستقبل رسائل من عملائي منذ أمس الساعة 3 مساءً..."
            rows={4}
            maxLength={500}
            className="w-full border border-slate-200 rounded-xl px-3 py-2.5 text-sm focus:outline-none focus:ring-2 focus:ring-amber-400 resize-none"
          />
          <p className="text-xs text-slate-400 text-left" dir="ltr">{description.length}/500</p>
        </div>

        {/* TTL */}
        <div className="space-y-2">
          <label className="text-xs font-semibold text-slate-700">مدة الوصول المسموحة</label>
          <div className="flex gap-2">
            {TTL_OPTS_HELP.map(o => (
              <button
                key={o.value}
                onClick={() => setTtlHours(o.value)}
                className={`flex-1 py-2 rounded-xl text-xs font-medium border transition ${
                  ttlHours === o.value
                    ? 'bg-amber-500 text-white border-amber-500'
                    : 'border-slate-200 text-slate-600 hover:bg-amber-50 hover:border-amber-300'
                }`}
              >
                {o.label}
              </button>
            ))}
          </div>
        </div>

        <div className="bg-blue-50 border border-blue-200 rounded-xl px-3 py-2 text-xs text-blue-700">
          🔒 لن يتمكن الدعم من الدخول إلا بعد موافقتك. ستصلك إشعار بالطلب.
        </div>

        {error && (
          <div className="text-sm text-red-600 bg-red-50 border border-red-200 rounded-xl px-3 py-2">{error}</div>
        )}

        <div className="flex gap-3">
          <button onClick={onClose} className="flex-1 py-2.5 border border-slate-200 rounded-xl text-sm text-slate-600 hover:bg-slate-50 transition">
            إلغاء
          </button>
          <button
            onClick={submit}
            disabled={busy || description.trim().length < 10}
            className="flex-1 flex items-center justify-center gap-2 py-2.5 bg-amber-500 hover:bg-amber-600 disabled:opacity-60 text-white font-bold rounded-xl text-sm transition"
          >
            {busy ? <><Loader2 className="w-4 h-4 animate-spin" /> إرسال...</> : <><Send className="w-4 h-4" /> إرسال الطلب</>}
          </button>
        </div>
      </div>
    </div>
  )
}

// ── Support History Panel ──────────────────────────────────────────────────────

function SupportHistoryPanel() {
  const [history, setHistory]   = useState<AccessRequest[]>([])
  const [loading, setLoading]   = useState(true)
  const [open, setOpen]         = useState(false)

  const load = useCallback(async () => {
    try {
      const res = await fetch(`${API_BASE}/merchant/support-history`, {
        headers: { Authorization: `Bearer ${localStorage.getItem('nahla_token') ?? ''}` },
      })
      if (res.ok) setHistory((await res.json()).requests ?? [])
    } catch { /* ignore */ }
    finally { setLoading(false) }
  }, [])

  useEffect(() => { load() }, [load])

  if (loading) return null
  if (history.length === 0) return (
    <div className="text-center text-xs text-slate-400 py-6">لا يوجد سجل طلبات بعد</div>
  )

  const statusColor = (s: string) => ({
    pending:  'bg-yellow-100 text-yellow-700',
    approved: 'bg-green-100 text-green-700',
    rejected: 'bg-red-100 text-red-700',
  }[s] ?? 'bg-slate-100 text-slate-500')

  const statusLabel = (s: string) => ({
    pending:  'معلّق',
    approved: 'موافق عليه',
    rejected: 'مرفوض',
  }[s] ?? s)

  return (
    <div className="space-y-2">
      <button
        onClick={() => setOpen(o => !o)}
        className="flex items-center gap-2 text-xs font-semibold text-slate-600 hover:text-slate-800"
      >
        <History className="w-3.5 h-3.5" />
        سجل الطلبات السابقة ({history.length})
        <ChevronDown className={`w-3.5 h-3.5 transition-transform ${open ? 'rotate-180' : ''}`} />
      </button>
      {open && (
        <div className="space-y-2 max-h-64 overflow-y-auto pr-1">
          {history.map(r => (
            <div key={r.id} className="flex items-start gap-3 bg-slate-50 rounded-xl px-3 py-2.5">
              <span className={`text-xs px-2 py-0.5 rounded-full font-medium shrink-0 mt-0.5 ${statusColor(r.status ?? '')}`}>
                {statusLabel(r.status ?? '')}
              </span>
              <div className="min-w-0 flex-1">
                <p className="text-xs font-medium text-slate-700 truncate">
                  {r.reason || '—'}
                </p>
                <p className="text-xs text-slate-400 mt-0.5">
                  {r.ttl_hours ? `${r.ttl_hours} ساعة · ` : ''}
                  {new Date(r.requested_at).toLocaleString('ar-SA')}
                </p>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

// ── Merchant Help Request Status (shows OWN pending request) ─────────────────

function MerchantHelpStatus({ onCancel }: { onCancel?: () => void }) {
  const [data, setData]         = useState<null | { has_pending: boolean; reason?: string; ttl_hours?: number; requested_at?: string }>(null)
  const [loading, setLoading]   = useState(true)
  const [cancelling, setCancelling] = useState(false)
  const [cancelled, setCancelled]   = useState(false)

  const load = useCallback(async () => {
    try {
      const res = await fetch(`${API_BASE}/merchant/my-help-request`, {
        headers: { Authorization: `Bearer ${localStorage.getItem('nahla_token') ?? ''}` },
      })
      if (res.ok) setData(await res.json())
    } catch { /* ignore */ }
    finally { setLoading(false) }
  }, [])

  useEffect(() => { load() }, [load])

  const handleCancel = async () => {
    if (!window.confirm('هل تريد إلغاء طلب المساعدة؟ يمكنك إرسال طلب جديد في أي وقت.')) return
    setCancelling(true)
    try {
      const res = await fetch(`${API_BASE}/merchant/my-help-request`, {
        method: 'DELETE',
        headers: { Authorization: `Bearer ${localStorage.getItem('nahla_token') ?? ''}` },
      })
      if (res.ok) {
        setCancelled(true)
        setData(null)
        onCancel?.()
        setTimeout(() => setCancelled(false), 4000)
      }
    } catch { /* ignore */ }
    finally { setCancelling(false) }
  }

  if (loading) return null

  if (cancelled) {
    return (
      <div className="flex items-center gap-2 bg-slate-50 border border-slate-200 rounded-xl px-4 py-3 text-sm text-slate-500">
        <CheckCircle className="w-4 h-4 shrink-0" />
        تم إلغاء طلب المساعدة.
      </div>
    )
  }

  if (!data?.has_pending) return null

  return (
    <div className="bg-blue-50 border border-blue-200 rounded-xl px-4 py-3" dir="rtl">
      <div className="flex items-start gap-3">
        <Clock className="w-4 h-4 text-blue-500 shrink-0 mt-0.5" />
        <div className="flex-1 min-w-0">
          <p className="text-sm font-semibold text-blue-800">طلبك وصل لفريق نحلة ✓</p>
          <p className="text-xs text-blue-600 mt-0.5 leading-relaxed">
            {data.reason && <span className="block">{data.reason}</span>}
            سيتواصل معك الفريق قريباً — لا تحتاج لأي إجراء إضافي.
          </p>
          {data.requested_at && (
            <p className="text-xs text-blue-400 mt-1">
              {new Intl.DateTimeFormat('ar-SA', { dateStyle: 'short', timeStyle: 'short' }).format(new Date(data.requested_at))}
            </p>
          )}
        </div>
        <button
          onClick={handleCancel}
          disabled={cancelling}
          className="shrink-0 text-xs text-red-500 hover:text-red-700 hover:underline disabled:opacity-50 mt-0.5"
        >
          {cancelling ? 'جارٍ الإلغاء…' : 'إلغاء الطلب'}
        </button>
      </div>
    </div>
  )
}

// ── Full Support Tab ───────────────────────────────────────────────────────────

function SupportTab() {
  const [showModal, setShowModal]   = useState(false)
  const [sent, setSent]             = useState(false)
  const [hasMerchantPending, setMerchantPending] = useState(false)

  // Check if merchant already has a pending help request (their own)
  const checkMyPending = useCallback(async () => {
    try {
      const res = await fetch(`${API_BASE}/merchant/my-help-request`, {
        headers: { Authorization: `Bearer ${localStorage.getItem('nahla_token') ?? ''}` },
      })
      if (res.ok) {
        const d = await res.json()
        setMerchantPending(d.has_pending ?? false)
      }
    } catch { /* ignore */ }
  }, [])

  useEffect(() => { checkMyPending() }, [checkMyPending])

  const handleSent = () => {
    setShowModal(false)
    setSent(true)
    setMerchantPending(true)
    setTimeout(() => setSent(false), 6000)
  }

  return (
    <div className="space-y-6">
      {/* Section 1: Request Help */}
      <div className="card p-5 space-y-4">
        <div className="flex items-start gap-3">
          <div className="w-9 h-9 bg-amber-100 rounded-xl flex items-center justify-center shrink-0">
            <HeadphonesIcon className="w-5 h-5 text-amber-600" />
          </div>
          <div className="flex-1">
            <h3 className="text-sm font-bold text-slate-900">طلب مساعدة من نحلة</h3>
            <p className="text-xs text-slate-500 mt-0.5">
              فريق نحلة يساعدك داخل المنصة مباشرة. لا واتساب، لا إيميل — كل شيء موثّق.
            </p>
          </div>
        </div>

        {sent && (
          <div className="flex items-center gap-2 bg-green-50 border border-green-200 rounded-xl px-4 py-3 text-sm text-green-700">
            <CheckCircle className="w-4 h-4 shrink-0" />
            تم إرسال طلبك! أُبلغ فريق نحلة وسيتواصل معك قريباً.
          </div>
        )}

        {/* Status of merchant's own pending request (with cancel button) */}
        <MerchantHelpStatus onCancel={() => { setMerchantPending(false); checkMyPending() }} />

        <button
          onClick={() => setShowModal(true)}
          disabled={hasMerchantPending}
          className="w-full flex items-center justify-center gap-2 py-3 bg-amber-500 hover:bg-amber-600 disabled:opacity-50 disabled:cursor-not-allowed text-white font-bold rounded-xl text-sm transition"
        >
          <HeadphonesIcon className="w-4 h-4" />
          {hasMerchantPending ? '⏳ طلبك قيد المراجعة من فريق نحلة' : 'طلب مساعدة من نحلة'}
        </button>

        {/* How it works */}
        <div className="bg-slate-50 rounded-xl p-3 space-y-2">
          <p className="text-xs font-semibold text-slate-600">كيف يعمل؟</p>
          <div className="space-y-1.5">
            {[
              { n: '١', t: 'تصف مشكلتك وتختار نوعها' },
              { n: '٢', t: 'يصل إشعار فوري لفريق نحلة بالإيميل ولوحة المالك' },
              { n: '٣', t: 'فريق الدعم يراجع ويتواصل معك' },
              { n: '٤', t: 'كل نشاط مُسجَّل ومرئي لك في أي وقت' },
            ].map(s => (
              <div key={s.n} className="flex items-center gap-2 text-xs text-slate-600">
                <span className="w-5 h-5 rounded-full bg-amber-100 text-amber-700 font-bold flex items-center justify-center shrink-0 text-xs">
                  {s.n}
                </span>
                {s.t}
              </div>
            ))}
          </div>
        </div>
      </div>

      {/* Section 2: Incoming Requests (from admin) */}
      <AccessRequestsPanel />

      {/* Section 3: Active access status */}
      <SupportAccessStatusSection />

      {/* Section 4: History */}
      <div className="card p-5 space-y-3">
        <div className="flex items-center gap-2">
          <History className="w-4 h-4 text-slate-400" />
          <h3 className="text-sm font-semibold text-slate-700">سجل الوصول</h3>
        </div>
        <SupportHistoryPanel />
      </div>

      {showModal && (
        <HelpRequestModal
          onClose={() => setShowModal(false)}
          onSent={handleSent}
        />
      )}
    </div>
  )
}

// ── Compact support access status (for Support tab) ───────────────────────────

function SupportAccessStatusSection() {
  const [status, setStatus] = useState<{
    enabled: boolean; expires_at: string | null; granted_at: string | null
  } | null>(null)
  const [saving, setSaving] = useState(false)

  const load = useCallback(async () => {
    try {
      const res = await fetch(`${API_BASE}/merchant/support-access`, {
        headers: { Authorization: `Bearer ${localStorage.getItem('nahla_token') ?? ''}` },
      })
      if (res.ok) setStatus(await res.json())
    } catch { /* ignore */ }
  }, [])

  useEffect(() => { load() }, [load])

  const revoke = async () => {
    setSaving(true)
    try {
      await fetch(`${API_BASE}/merchant/support-access/disable`, {
        method: 'POST',
        headers: { Authorization: `Bearer ${localStorage.getItem('nahla_token') ?? ''}` },
      })
      await load()
    } catch { /* ignore */ }
    finally { setSaving(false) }
  }

  if (!status?.enabled) return null

  const fmt = (iso: string | null) => {
    if (!iso) return '—'
    try {
      return new Intl.DateTimeFormat('ar-SA', { timeStyle: 'short', dateStyle: 'short', timeZone: 'Asia/Riyadh' }).format(new Date(iso))
    } catch { return iso }
  }

  return (
    <div className="rounded-2xl border-2 border-red-400 bg-red-50 overflow-hidden">
      <div className="flex items-center gap-3 bg-red-600 px-5 py-3">
        <span className="relative flex h-2.5 w-2.5">
          <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-white opacity-60" />
          <span className="relative inline-flex rounded-full h-2.5 w-2.5 bg-white" />
        </span>
        <p className="text-white font-bold text-sm flex-1">⚠️ وصول الدعم نشط الآن</p>
      </div>
      <div className="px-5 py-4 space-y-3">
        <div className="grid grid-cols-2 gap-2 text-xs">
          <div className="bg-white rounded-lg p-2.5 border border-red-100">
            <p className="text-slate-400 mb-0.5">ينتهي في</p>
            <p className="font-bold text-red-700">{fmt(status.expires_at)}</p>
          </div>
          <div className="bg-white rounded-lg p-2.5 border border-red-100">
            <p className="text-slate-400 mb-0.5">مُفعَّل منذ</p>
            <p className="font-bold text-slate-700">{fmt(status.granted_at)}</p>
          </div>
        </div>
        <button
          onClick={revoke}
          disabled={saving}
          className="w-full flex items-center justify-center gap-2 py-2.5 rounded-xl bg-red-600 hover:bg-red-700 text-white font-bold text-sm transition disabled:opacity-60"
        >
          {saving ? <Loader2 className="w-4 h-4 animate-spin" /> : <ShieldOff className="w-4 h-4" />}
          إلغاء الوصول فوراً
        </button>
      </div>
    </div>
  )
}

// Small badge for pending support requests on the tab ─────────────────────────

function SupportTabBadge() {
  const [count, setCount] = useState(0)

  useEffect(() => {
    const load = async () => {
      try {
        const res = await fetch(`${API_BASE}/merchant/access-requests`, {
          headers: { Authorization: `Bearer ${localStorage.getItem('nahla_token') ?? ''}` },
        })
        if (res.ok) setCount((await res.json()).count ?? 0)
      } catch { /* ignore */ }
    }
    load()
    const id = setInterval(load, 30_000)
    return () => clearInterval(id)
  }, [])

  if (count === 0) return null
  return (
    <span className="inline-flex items-center justify-center w-4 h-4 rounded-full bg-red-500 text-white text-[10px] font-bold shrink-0">
      {count}
    </span>
  )
}

// ─────────────────────────────────────────────────────────────────────────────

interface AccessRequest {
  id: string
  requested_by: string
  requested_at: string
  store_name: string
  status?: string
  reason?: string
  ttl_hours?: number
  merchant_email?: string
}

const TTL_OPTS_APPROVE = [
  { value: 1,  label: 'ساعة'    },
  { value: 2,  label: 'ساعتان'  },
  { value: 4,  label: '4 ساعات' },
  { value: 8,  label: '8 ساعات' },
  { value: 24, label: '24 ساعة' },
  { value: 48, label: '48 ساعة' },
]

function AccessRequestsPanel({ onApproved }: { onApproved?: () => void }) {
  const [requests, setRequests]     = useState<AccessRequest[]>([])
  const [loading, setLoading]       = useState(true)
  const [responding, setResponding] = useState<string | null>(null)
  // Per-request TTL (defaults to what admin requested, or 4)
  const [ttlMap, setTtlMap]         = useState<Record<string, number>>({})

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const res = await fetch(`${API_BASE}/merchant/access-requests`, {
        headers: { Authorization: `Bearer ${localStorage.getItem('nahla_token') ?? ''}` },
      })
      if (res.ok) {
        const d = await res.json()
        const reqs: AccessRequest[] = d.requests ?? []
        setRequests(reqs)
        // Init TTL map from requested TTL (or default 4)
        const init: Record<string, number> = {}
        reqs.forEach(r => { init[r.id] = r.ttl_hours ?? 4 })
        setTtlMap(prev => ({ ...init, ...prev }))
      }
    } catch { /* ignore */ }
    finally { setLoading(false) }
  }, [])

  useEffect(() => { load() }, [load])

  const respond = async (reqId: string, approve: boolean) => {
    setResponding(reqId)
    try {
      const ttl = ttlMap[reqId] ?? 4
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
          window.dispatchEvent(new Event('nahla:support-access-changed'))
          if (onApproved) onApproved()
        }
      }
    } catch { /* ignore */ }
    finally { setResponding(null) }
  }

  if (loading || requests.length === 0) return null

  return (
    <div className="card p-5 border-amber-300 bg-amber-50">
      <div className="flex items-center gap-2 mb-4">
        <Bell className="w-4 h-4 text-amber-600" />
        <h4 className="text-sm font-bold text-amber-900">
          طلبات وصول معلّقة ({requests.length})
        </h4>
      </div>
      <div className="space-y-4">
        {requests.map(r => (
          <div key={r.id} className="bg-white rounded-xl border border-amber-200 overflow-hidden">
            {/* Request header */}
            <div className="px-4 py-3 border-b border-amber-100 bg-amber-50/60">
              <div className="flex items-start gap-2">
                <ShieldCheck className="w-4 h-4 text-amber-600 shrink-0 mt-0.5" />
                <div>
                  <p className="text-sm font-bold text-slate-800">
                    فريق نحلة يطلب الوصول المؤقت إلى لوحتك
                  </p>
                  <p className="text-xs text-slate-500 mt-0.5">
                    بواسطة: <span className="font-medium">{r.requested_by}</span>
                    {' · '}
                    {new Date(r.requested_at).toLocaleString('ar-SA')}
                  </p>
                </div>
              </div>
            </div>

            {/* Details */}
            <div className="px-4 py-3 space-y-3">
              {/* Reason */}
              {r.reason && (
                <div className="bg-slate-50 rounded-lg px-3 py-2.5">
                  <p className="text-xs text-slate-400 mb-0.5">سبب الطلب</p>
                  <p className="text-sm font-semibold text-slate-800">{r.reason}</p>
                </div>
              )}

              {/* Requested TTL */}
              {r.ttl_hours && (
                <div className="flex items-center gap-2 text-xs text-slate-500">
                  <span>المدة المطلوبة من فريق الدعم:</span>
                  <span className="font-semibold text-amber-700">
                    {r.ttl_hours} ساعة
                  </span>
                </div>
              )}

              {/* Approve TTL chooser */}
              <div>
                <p className="text-xs text-slate-500 mb-2">المدة التي ستمنحها:</p>
                <div className="flex flex-wrap gap-1.5">
                  {TTL_OPTS_APPROVE.map(o => (
                    <button
                      key={o.value}
                      onClick={() => setTtlMap(prev => ({ ...prev, [r.id]: o.value }))}
                      className={`px-2.5 py-1 text-xs rounded-lg border transition-colors ${
                        (ttlMap[r.id] ?? r.ttl_hours ?? 4) === o.value
                          ? 'bg-amber-500 text-white border-amber-500'
                          : 'border-slate-200 text-slate-600 hover:bg-amber-50'
                      }`}
                    >
                      {o.label}
                    </button>
                  ))}
                </div>
              </div>

              {/* Action buttons */}
              <div className="flex gap-2 pt-1">
                <button
                  onClick={() => respond(r.id, true)}
                  disabled={responding === r.id}
                  className="flex-1 flex items-center justify-center gap-1.5 py-2 bg-green-600 hover:bg-green-700 text-white rounded-xl text-xs font-bold transition disabled:opacity-60"
                >
                  {responding === r.id
                    ? <Loader2 className="w-3.5 h-3.5 animate-spin" />
                    : <ShieldCheck className="w-3.5 h-3.5" />}
                  ✅ موافقة
                </button>
                <button
                  onClick={() => respond(r.id, false)}
                  disabled={responding === r.id}
                  className="flex-1 flex items-center justify-center gap-1.5 py-2 border border-red-200 text-red-600 hover:bg-red-50 rounded-xl text-xs font-bold transition disabled:opacity-60"
                >
                  <ShieldOff className="w-3.5 h-3.5" />
                  ❌ رفض
                </button>
              </div>
            </div>
          </div>
        ))}
      </div>
    </div>
  )
}

// ── Tab: System Info ─────────────────────────────────────────────────────────

function SystemInfoTab() {
  const { lang } = useLanguage()
  const isAr = lang === 'ar'

  const tenantId  = localStorage.getItem('nahla_tenant_id')  ?? '—'
  const userId    = localStorage.getItem('nahla_user_id')    ?? '—'
  const role      = localStorage.getItem('nahla_role')       ?? '—'
  const email     = localStorage.getItem('nahla_email')      ?? '—'

  const rows = [
    { label: isAr ? 'Tenant ID'    : 'Tenant ID',    value: tenantId,                 copy: tenantId  !== '—' },
    { label: isAr ? 'User ID'      : 'User ID',      value: userId,                   copy: false },
    { label: isAr ? 'الدور'        : 'Role',         value: role,                     copy: false },
    { label: isAr ? 'البريد'       : 'Email',        value: email,                    copy: false },
    { label: isAr ? 'الخادم'       : 'API Base',     value: import.meta.env.VITE_API_BASE ?? 'https://api.nahlah.ai', copy: false },
  ]

  const [copied, setCopied] = useState<string | null>(null)
  const doCopy = (v: string) => {
    navigator.clipboard.writeText(v).then(() => {
      setCopied(v)
      setTimeout(() => setCopied(null), 1500)
    })
  }

  return (
    <div className="space-y-4">
      <Section
        title={isAr ? 'معلومات الجلسة الحالية' : 'Current Session Info'}
        description={isAr
          ? 'بيانات تقنية مفيدة عند التشخيص وتتبع مشاكل الربط.'
          : 'Technical identifiers useful for debugging integration issues.'}
      >
        <div className="divide-y divide-slate-100">
          {rows.map(({ label, value, copy }) => (
            <div key={label} className="flex items-center justify-between py-2.5 gap-4">
              <span className="text-xs text-slate-500 w-32 shrink-0">{label}</span>
              <div className="flex items-center gap-2 min-w-0">
                <span className="text-sm font-mono font-semibold text-slate-800 truncate">{value}</span>
                {copy && (
                  <button
                    onClick={() => doCopy(value)}
                    className="text-xs text-brand-600 hover:text-brand-700 shrink-0 flex items-center gap-1 px-1.5 py-0.5 rounded border border-brand-200 hover:bg-brand-50 transition-colors"
                  >
                    <Copy className="w-3 h-3" />
                    {copied === value ? (isAr ? 'تم' : 'Copied') : (isAr ? 'نسخ' : 'Copy')}
                  </button>
                )}
              </div>
            </div>
          ))}
        </div>
      </Section>

      <Section
        title={isAr ? 'ما هو Tenant ID؟' : 'What is Tenant ID?'}
        description={isAr
          ? 'يُستخدم عند الإبلاغ عن مشاكل أو التحقق من ربط واتساب وسلة.'
          : 'Used when reporting issues or verifying WhatsApp and Salla integration routing.'}
      >
        <p className="text-xs text-slate-500 leading-relaxed">
          {isAr
            ? 'كل متجر في نحلة له معرّف فريد (Tenant ID). جميع بيانات الواتساب والمتجر والمحادثات يجب أن تنتمي لنفس هذا المعرّف. إذا رأيت خطأ في التوجيه أو الرسائل، أعطِ فريق الدعم هذا الرقم.'
            : 'Every store in Nahla has a unique Tenant ID. All WhatsApp connections, store integrations, and conversation data must belong to this same ID. When reporting routing or message delivery issues, share this number with the support team.'}
        </p>
      </Section>
    </div>
  )
}

// ── Main Settings page ───────────────────────────────────────────────────────

export default function Settings() {
  const { t } = useLanguage()
  const [searchParams, setSearchParams] = useSearchParams()
  const _isOwner = isPlatformOwner()

  // Resolve initial tab from ?tab=... URL param
  const _tabParam = searchParams.get('tab') as TabId | null
  const _defaultTab: TabId = (_tabParam && TAB_IDS.includes(_tabParam as TabId)) ? _tabParam : 'team'
  const [activeTab, setActiveTab] = useState<TabId>(_defaultTab)

  const [settings, setSettings] = useState<AllSettings | null>(null)
  const [loading, setLoading] = useState(true)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)
  const [savedTab, setSavedTab] = useState<TabId | null>(null)
  const [saveError, setSaveError] = useState<string | null>(null)

  const TABS = TAB_IDS
    // Hide 'support' tab for platform owner (admin), hide 'security' for owner too
    .filter(id => {
      if (_isOwner && (id === 'security' || id === 'support')) return false
      return true
    })
    .map(id => ({
      id,
      // Use our own label map for the new 'support' tab, fallback to i18n for others
      label: TAB_LABELS[id] ?? t(tr => tr.settings.tabs[id as keyof typeof tr.settings.tabs]),
      icon: TAB_ICONS[id],
    }))

  const handleTabChange = (id: TabId) => {
    setActiveTab(id)
    setSearchParams(id !== 'team' ? { tab: id } : {})
  }

  useEffect(() => {
    settingsApi.getAll()
      .then(setSettings)
      .catch(() => setLoadError(t(tr => tr.meta.code) === 'ar'
        ? 'تعذّر تحميل الإعدادات. تأكد من تشغيل الخادم الخلفي.'
        : 'Failed to load settings. Make sure the backend is running.'))
      .finally(() => setLoading(false))
  }, []) // eslint-disable-line react-hooks/exhaustive-deps

  const patchNotifications = (patch: Partial<NotificationSettings>) =>
    setSettings(s => s ? { ...s, notifications: { ...s.notifications, ...patch } } : s)

  const handleSave = async (tab: TabId) => {
    if (!settings) return
    setSaving(true)
    setSaveError(null)
    setSavedTab(null)
    try {
      const updated = await settingsApi.update({
        notifications: tab === 'notifications' ? settings.notifications : undefined,
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

      {/* Quick access to dedicated feature pages */}
      <QuickAccess />

      {/* Tab bar */}
      <div className="border-b border-slate-200 -mx-3 px-3 md:-mx-6 md:px-6">
        <div className="flex gap-1 overflow-x-auto">
          {TABS.map(({ id, label, icon: Icon }) => (
            <button
              key={id}
              onClick={() => handleTabChange(id)}
              className={`flex items-center gap-2 px-4 py-2.5 text-sm font-medium whitespace-nowrap transition-colors border-b-2 -mb-px ${
                activeTab === id
                  ? 'border-brand-500 text-brand-600'
                  : 'border-transparent text-slate-500 hover:text-slate-700 hover:border-slate-300'
              }`}
            >
              <Icon className="w-4 h-4 shrink-0" />
              {label}
              {id === 'support' && <SupportTabBadge />}
            </button>
          ))}
        </div>
      </div>

      {/* Tab content */}
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
      {activeTab === 'support' && !_isOwner && <SupportTab />}
      {activeTab === 'security' && !_isOwner && <SupportAccessTab />}
      {activeTab === 'system' && <SystemInfoTab />}
    </div>
  )
}
