/**
 * AdminDashboard.tsx
 * SaaS owner dashboard — Nahlah AI platform overview.
 * Focused entirely on platform-level metrics, NOT individual merchant store data.
 */
import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { adminApi, type AdminPlatformStats } from '../api/admin'
import {
  Users, TrendingUp, DollarSign, Zap, AlertTriangle,
  CheckCircle, Store, Wifi, WifiOff, UserX, Clock,
  ShieldAlert, ArrowUpRight, RefreshCw, Loader2,
  BarChart3, Activity, Crown, ChevronRight,
} from 'lucide-react'

// ── Primitive components ───────────────────────────────────────────────────────

function KpiCard({
  label, value, sub, icon: Icon, accent, prefix = '', suffix = '', alert = false,
}: {
  label: string; value: string | number; sub?: string
  icon: React.ElementType; accent: string
  prefix?: string; suffix?: string; alert?: boolean
}) {
  return (
    <div className={`bg-white rounded-2xl border p-5 flex flex-col gap-3 shadow-sm hover:shadow-md transition-shadow ${alert && Number(value) > 0 ? 'border-amber-200 bg-amber-50/30' : 'border-slate-100'}`}>
      <div className="flex items-center justify-between">
        <span className="text-slate-500 text-xs font-medium leading-tight">{label}</span>
        <div className={`w-9 h-9 rounded-xl flex items-center justify-center ${accent}`}>
          <Icon className="w-4 h-4 text-white" />
        </div>
      </div>
      <div>
        <div className={`text-2xl font-black ${alert && Number(value) > 0 ? 'text-amber-700' : 'text-slate-800'}`}>
          {prefix}{typeof value === 'number' ? value.toLocaleString('ar-SA') : value}{suffix}
        </div>
        {sub && <div className="text-xs text-slate-400 mt-0.5">{sub}</div>}
      </div>
    </div>
  )
}

function SectionHeader({ title, subtitle, linkTo, linkLabel }: {
  title: string; subtitle?: string; linkTo?: string; linkLabel?: string
}) {
  const navigate = useNavigate()
  return (
    <div className="flex items-end justify-between mb-4">
      <div>
        <h2 className="text-sm font-bold text-slate-800">{title}</h2>
        {subtitle && <p className="text-xs text-slate-400 mt-0.5">{subtitle}</p>}
      </div>
      {linkTo && (
        <button
          onClick={() => navigate(linkTo)}
          className="text-xs text-amber-600 hover:text-amber-700 font-medium flex items-center gap-0.5"
        >
          {linkLabel ?? 'عرض الكل'} <ChevronRight className="w-3 h-3" />
        </button>
      )}
    </div>
  )
}

function PlanBar({ name, count, total, price }: { name: string; count: number; total: number; price: number }) {
  const pct = total > 0 ? Math.round((count / total) * 100) : 0
  return (
    <div className="space-y-1.5">
      <div className="flex items-center justify-between text-xs">
        <span className="font-medium text-slate-700">{name}</span>
        <span className="text-slate-500">{count} تاجر · {price.toLocaleString('ar-SA')} ر.س/شهر</span>
      </div>
      <div className="h-2 bg-slate-100 rounded-full overflow-hidden">
        <div
          className="h-full bg-gradient-to-r from-amber-400 to-amber-500 rounded-full transition-all"
          style={{ width: `${pct}%` }}
        />
      </div>
    </div>
  )
}

function FunnelStep({
  label, count, total, icon: Icon, color, description,
}: {
  label: string; count: number; total: number; icon: React.ElementType; color: string; description?: string
}) {
  const pct = total > 0 ? Math.round((count / total) * 100) : 0
  return (
    <div className="flex items-center gap-3 py-2.5 border-b border-slate-50 last:border-0">
      <div className={`w-8 h-8 rounded-lg flex items-center justify-center shrink-0 ${color}`}>
        <Icon className="w-3.5 h-3.5 text-white" />
      </div>
      <div className="flex-1 min-w-0">
        <div className="flex items-center justify-between">
          <span className="text-xs font-medium text-slate-700">{label}</span>
          <span className="text-sm font-bold text-slate-800">{count.toLocaleString('ar-SA')}</span>
        </div>
        {description && <p className="text-xs text-slate-400 mt-0.5">{description}</p>}
        <div className="h-1 bg-slate-100 rounded-full mt-1.5 overflow-hidden">
          <div className="h-full bg-gradient-to-r from-violet-400 to-violet-500 rounded-full" style={{ width: `${pct}%` }} />
        </div>
      </div>
      <span className="text-xs text-slate-400 shrink-0 w-8 text-left">{pct}%</span>
    </div>
  )
}

function AtRiskBanner({
  label, count, sub, icon: Icon, color, linkTo, linkLabel,
}: {
  label: string; count: number; sub: string; icon: React.ElementType
  color: string; linkTo?: string; linkLabel?: string
}) {
  const navigate = useNavigate()
  if (count === 0) return null
  return (
    <div className={`flex items-center gap-3 px-4 py-3 rounded-xl border ${color}`}>
      <Icon className="w-4 h-4 shrink-0" />
      <div className="flex-1 min-w-0">
        <p className="text-sm font-semibold">{label}</p>
        <p className="text-xs opacity-75 mt-0.5">{sub}</p>
      </div>
      <span className="text-xl font-black shrink-0">{count}</span>
      {linkTo && (
        <button
          onClick={() => navigate(linkTo)}
          className="shrink-0 flex items-center gap-1 text-xs font-medium underline underline-offset-2 hover:opacity-80"
        >
          {linkLabel ?? 'عرض'} <ArrowUpRight className="w-3 h-3" />
        </button>
      )}
    </div>
  )
}

function QuickAction({ label, sub, icon: Icon, to }: { label: string; sub: string; icon: React.ElementType; to: string }) {
  const navigate = useNavigate()
  return (
    <button
      onClick={() => navigate(to)}
      className="flex flex-col gap-2 p-4 bg-white rounded-xl border border-slate-100 hover:border-amber-200 hover:bg-amber-50/20 text-right transition-all group shadow-sm"
    >
      <div className="w-8 h-8 rounded-lg bg-slate-100 group-hover:bg-amber-100 flex items-center justify-center transition-colors">
        <Icon className="w-4 h-4 text-slate-500 group-hover:text-amber-600 transition-colors" />
      </div>
      <div>
        <p className="text-xs font-bold text-slate-800">{label}</p>
        <p className="text-xs text-slate-400 mt-0.5">{sub}</p>
      </div>
    </button>
  )
}

function StatusBadge({ status }: { status: string }) {
  const map: Record<string, { label: string; cls: string }> = {
    active:        { label: 'نشط',       cls: 'bg-green-100 text-green-700' },
    trialing:      { label: 'تجربة',     cls: 'bg-blue-100 text-blue-700'  },
    canceled:      { label: 'ملغى',      cls: 'bg-red-100 text-red-700'    },
    none:          { label: 'بلا باقة',  cls: 'bg-slate-100 text-slate-500'},
    connected:     { label: 'مربوط',     cls: 'bg-green-100 text-green-700'},
    not_connected: { label: 'غير مربوط', cls: 'bg-slate-100 text-slate-400'},
    paid:          { label: 'مدفوع',     cls: 'bg-green-100 text-green-700'},
    pending:       { label: 'معلق',      cls: 'bg-yellow-100 text-yellow-700'},
    failed:        { label: 'فاشل',      cls: 'bg-red-100 text-red-700'    },
  }
  const s = map[status] ?? { label: status, cls: 'bg-slate-100 text-slate-500' }
  return <span className={`text-xs px-2 py-0.5 rounded-full font-medium ${s.cls}`}>{s.label}</span>
}

// ── Main page ──────────────────────────────────────────────────────────────────

export default function AdminDashboard() {
  const [stats, setStats]     = useState<AdminPlatformStats | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError]     = useState('')
  const [refreshed, setRefreshed] = useState<Date>(new Date())

  const load = () => {
    setLoading(true)
    setError('')
    adminApi.stats()
      .then(s => { setStats(s); setRefreshed(new Date()) })
      .catch((e: unknown) => setError(e instanceof Error ? e.message : 'خطأ في تحميل البيانات'))
      .finally(() => setLoading(false))
  }

  useEffect(() => { load() }, [])

  if (loading) return (
    <div className="flex items-center justify-center h-64 gap-3 text-slate-400">
      <Loader2 className="w-6 h-6 animate-spin text-amber-500" />
      <span className="text-sm">جارٍ تحميل بيانات المنصة...</span>
    </div>
  )

  if (error) return (
    <div className="flex flex-col items-center justify-center h-64 gap-3">
      <AlertTriangle className="w-8 h-8 text-red-400" />
      <p className="text-sm text-red-600">{error}</p>
      <button onClick={load} className="btn-secondary text-xs">
        <RefreshCw className="w-3.5 h-3.5" /> إعادة المحاولة
      </button>
    </div>
  )

  if (!stats) return null

  const byPlan   = stats.subscriptions.by_plan ?? {}
  const onboard  = stats.onboarding ?? { registered_only: 0, salla_only: 0, whatsapp_only: 0, both_connected: 0 }
  const atRisk   = stats.at_risk    ?? { trials_expiring_7d: 0, salla_needs_reauth: 0, suspended: 0 }
  const totalOnboard = onboard.registered_only + onboard.salla_only + onboard.whatsapp_only + onboard.both_connected
  const totalAtRisk  = atRisk.trials_expiring_7d + atRisk.salla_needs_reauth + atRisk.suspended
  const totalPlanMerchants = Object.values(byPlan).reduce((s, p) => s + p.count, 0)

  const now = refreshed.toLocaleString('ar-SA', { dateStyle: 'short', timeStyle: 'short' })

  return (
    <div className="space-y-6" dir="rtl">

      {/* ── Header ─────────────────────────────────────────────────────────── */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-3">
          <div className="w-11 h-11 rounded-2xl bg-amber-500 flex items-center justify-center shadow-lg shadow-amber-500/30">
            <Crown className="w-5 h-5 text-white" />
          </div>
          <div>
            <h1 className="text-xl font-black text-slate-800">لوحة مالك نحلة</h1>
            <p className="text-slate-400 text-xs">نظرة شاملة على المنصة · آخر تحديث: {now}</p>
          </div>
        </div>
        <button onClick={load} disabled={loading} className="btn-secondary text-xs py-1.5 disabled:opacity-50">
          <RefreshCw className={`w-3.5 h-3.5 ${loading ? 'animate-spin' : ''}`} />
          تحديث
        </button>
      </div>

      {/* ── Section 1: Executive KPIs ──────────────────────────────────────── */}
      <div>
        <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-6 gap-3">
          <KpiCard label="إجمالي التجار"    value={stats.merchants.total}      icon={Users}       accent="bg-blue-500"    sub={`${stats.merchants.active} نشط`} />
          <KpiCard label="التجربة المجانية" value={stats.merchants.trial}      icon={Clock}       accent="bg-sky-500"     sub="جارٍ التجربة" />
          <KpiCard label="مدفوعون"          value={stats.merchants.paid ?? 0}  icon={CheckCircle} accent="bg-emerald-500" sub="اشتراك نشط" />
          <KpiCard label="MRR"              value={(stats.revenue.mrr_sar).toFixed(0)} icon={TrendingUp} accent="bg-violet-500" suffix=" ر.س" sub="الإيرادات الشهرية" />
          <KpiCard label="إيرادات اليوم"   value={stats.revenue.today_sar.toFixed(0)} icon={DollarSign} accent="bg-green-500" suffix=" ر.س" sub="منذ منتصف الليل" />
          <KpiCard label="انضم هذا الأسبوع" value={stats.new_this_week ?? 0}  icon={Zap}         accent="bg-amber-500"   sub="تاجر جديد" />
        </div>
        <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 mt-3">
          <KpiCard label="ربطوا واتساب"   value={stats.wa_connected ?? 0}          icon={Wifi}        accent="bg-teal-500"   sub="واتساب مفعّل" />
          <KpiCard label="أكملوا الربطين" value={onboard.both_connected}             icon={Activity}    accent="bg-indigo-500" sub="سلة + واتساب" />
          <KpiCard label="موقوفون"        value={stats.merchants.suspended ?? 0}    icon={UserX}       accent="bg-red-400"    alert sub="حساب غير نشط" />
          <KpiCard label="مخاطر نشطة"    value={totalAtRisk}                        icon={AlertTriangle} accent="bg-orange-500" alert sub="تحتاج متابعة" />
        </div>
      </div>

      {/* ── Section 2: Plans + Onboarding Funnel ──────────────────────────── */}
      <div className="grid lg:grid-cols-2 gap-5">

        {/* Subscriptions & Plans */}
        <div className="bg-white rounded-2xl border border-slate-100 shadow-sm p-5">
          <SectionHeader
            title="الباقات والاشتراكات"
            subtitle="توزيع التجار على الباقات المدفوعة"
            linkTo="/admin/revenue"
            linkLabel="تقارير الإيرادات"
          />
          {Object.keys(byPlan).length === 0 ? (
            <div className="py-8 text-center text-slate-400 text-sm">لا توجد باقات مُفعّلة بعد</div>
          ) : (
            <div className="space-y-4">
              {Object.entries(byPlan).map(([slug, plan]) => (
                <PlanBar key={slug} name={plan.name_ar || slug} count={plan.count} total={totalPlanMerchants} price={plan.price} />
              ))}
              {stats.subscriptions.trial > 0 && (
                <PlanBar name="تجربة مجانية" count={stats.subscriptions.trial} total={totalPlanMerchants + stats.subscriptions.trial} price={0} />
              )}
            </div>
          )}
          <div className="mt-5 pt-4 border-t border-slate-50 grid grid-cols-3 gap-3 text-center text-xs">
            <div>
              <p className="text-lg font-black text-slate-800">{stats.subscriptions.active}</p>
              <p className="text-slate-400">اشتراك نشط</p>
            </div>
            <div>
              <p className="text-lg font-black text-slate-800">{stats.subscriptions.trial}</p>
              <p className="text-slate-400">في التجربة</p>
            </div>
            <div>
              <p className="text-lg font-black text-emerald-600">{stats.revenue.mrr_sar.toLocaleString('ar-SA')}</p>
              <p className="text-slate-400">MRR (ر.س)</p>
            </div>
          </div>
        </div>

        {/* Onboarding Funnel */}
        <div className="bg-white rounded-2xl border border-slate-100 shadow-sm p-5">
          <SectionHeader
            title="مسار تفعيل التجار"
            subtitle="أين وصل كل تاجر في رحلة الإعداد"
            linkTo="/admin/tenants"
            linkLabel="كل المتاجر"
          />
          <div>
            <FunnelStep
              label="أكمل الربطين"
              count={onboard.both_connected}
              total={totalOnboard}
              icon={CheckCircle}
              color="bg-emerald-500"
              description="سلة + واتساب — جاهز للعمل بالكامل"
            />
            <FunnelStep
              label="ربط سلة فقط"
              count={onboard.salla_only}
              total={totalOnboard}
              icon={Store}
              color="bg-blue-400"
              description="يحتاج ربط واتساب لإكمال التفعيل"
            />
            <FunnelStep
              label="ربط واتساب فقط"
              count={onboard.whatsapp_only}
              total={totalOnboard}
              icon={Wifi}
              color="bg-violet-400"
              description="يحتاج ربط سلة أو متجر إلكتروني"
            />
            <FunnelStep
              label="سجّل ولم يكمل"
              count={onboard.registered_only}
              total={totalOnboard}
              icon={WifiOff}
              color="bg-slate-400"
              description="لم يربط أياً من التكاملات بعد"
            />
          </div>
          <div className="mt-4 pt-3 border-t border-slate-50 text-center">
            <p className="text-xs text-slate-400">معدل الإكمال: <span className="font-bold text-slate-700">{totalOnboard > 0 ? Math.round((onboard.both_connected / totalOnboard) * 100) : 0}%</span> أكملوا الربطين</p>
          </div>
        </div>
      </div>

      {/* ── Section 3: At-Risk Alerts ──────────────────────────────────────── */}
      {totalAtRisk > 0 && (
        <div>
          <SectionHeader
            title="تنبيهات تحتاج متابعة"
            subtitle="تجار أو تكاملات بحاجة إلى تدخل"
          />
          <div className="space-y-2.5">
            <AtRiskBanner
              label="تجارب مجانية تنتهي خلال 7 أيام"
              count={atRisk.trials_expiring_7d}
              sub="تحتاج تواصل من فريق النجاح لتحويلهم إلى مشتركين"
              icon={Clock}
              color="border-amber-200 bg-amber-50 text-amber-800"
              linkTo="/admin/tenants"
              linkLabel="عرض التجار"
            />
            <AtRiskBanner
              label="متاجر سلة تحتاج إعادة توثيق"
              count={atRisk.salla_needs_reauth}
              sub="انتهت صلاحية الربط مع سلة — التاجر يحتاج إعادة تثبيت التطبيق"
              icon={ShieldAlert}
              color="border-red-200 bg-red-50 text-red-800"
              linkTo="/admin/troubleshooting"
              linkLabel="عرض التشخيص"
            />
            <AtRiskBanner
              label="حسابات موقوفة"
              count={atRisk.suspended}
              sub="تجار غير نشطين — قد يحتاجون مراجعة أو إعادة تفعيل"
              icon={UserX}
              color="border-slate-200 bg-slate-50 text-slate-700"
              linkTo="/admin/merchants"
              linkLabel="عرض التجار"
            />
          </div>
        </div>
      )}

      {/* ── Section 4: Recent Merchants + Quick Actions ────────────────────── */}
      <div className="grid lg:grid-cols-2 gap-5">

        {/* Recent signups */}
        <div className="bg-white rounded-2xl border border-slate-100 shadow-sm">
          <div className="px-5 py-4 border-b border-slate-50">
            <SectionHeader
              title="آخر التجار المسجلين"
              subtitle="آخر 8 تجار انضموا للمنصة"
              linkTo="/admin/tenants"
              linkLabel="كل المتاجر"
            />
          </div>
          <div className="divide-y divide-slate-50">
            {stats.recent_merchants.length === 0 ? (
              <p className="text-center py-10 text-slate-400 text-sm">لا يوجد تجار بعد</p>
            ) : stats.recent_merchants.map((m: any) => (
              <div key={m.id} className="px-5 py-3 flex items-center justify-between gap-3">
                <div className="min-w-0 flex items-center gap-2.5">
                  <div className="w-7 h-7 rounded-lg bg-slate-100 flex items-center justify-center shrink-0 text-xs font-bold text-slate-500">
                    {(m.store_name || m.email || '؟').charAt(0).toUpperCase()}
                  </div>
                  <div className="min-w-0">
                    <p className="text-xs font-semibold text-slate-700 truncate">{m.store_name || m.email}</p>
                    <p className="text-xs text-slate-400 truncate">{m.created_at ? new Date(m.created_at).toLocaleDateString('ar-SA') : '—'}</p>
                  </div>
                </div>
                <div className="flex items-center gap-1.5 shrink-0">
                  <StatusBadge status={m.sub_status ?? 'none'} />
                  <StatusBadge status={m.wa_status ?? 'not_connected'} />
                </div>
              </div>
            ))}
          </div>
        </div>

        {/* Quick Actions */}
        <div>
          <SectionHeader title="إجراءات إدارية سريعة" subtitle="روابط مباشرة للأقسام الرئيسية" />
          <div className="grid grid-cols-2 gap-3">
            <QuickAction label="كل المتاجر"         sub="إدارة وبحث التجار"         icon={Store}       to="/admin/tenants"      />
            <QuickAction label="التجار"             sub="الحسابات والصلاحيات"        icon={Users}       to="/admin/merchants"    />
            <QuickAction label="الإيرادات"          sub="MRR والمدفوعات"             icon={BarChart3}   to="/admin/revenue"      />
            <QuickAction label="استخدام الذكاء"     sub="استهلاك AI لكل متجر"        icon={Activity}    to="/admin/ai-usage"     />
            <QuickAction label="تشخيص المشاكل"      sub="أخطاء التكاملات والـ sync"  icon={ShieldAlert} to="/admin/troubleshooting" />
            <QuickAction label="صحة النظام"         sub="API والـ webhooks والـ DB"   icon={Wifi}        to="/admin/system"       />
            <QuickAction label="طلبات الـ Coexistence" sub="360dialog — تفعيل التجار" icon={Zap}      to="/admin/coexistence"  />
            <QuickAction label="الميزات التجريبية"  sub="Feature Flags للمنصة"       icon={TrendingUp}  to="/admin/features"     />
          </div>
        </div>
      </div>

      {/* ── Section 5: Revenue Snapshot ────────────────────────────────────── */}
      <div className="bg-gradient-to-r from-slate-800 to-slate-900 rounded-2xl p-5 text-white">
        <div className="flex flex-wrap gap-6 items-center justify-between">
          <div>
            <p className="text-slate-400 text-xs mb-1">منصة نحلة AI — ملخص إجمالي</p>
            <p className="font-black text-base">nahlah.ai</p>
          </div>
          {[
            { label: 'تجار',        val: stats.merchants.total },
            { label: 'مشتركون',     val: stats.subscriptions.active },
            { label: 'MRR',         val: `${stats.revenue.mrr_sar.toLocaleString('ar-SA')} ر.س` },
            { label: 'إجمالي',      val: `${stats.revenue.total_sar.toLocaleString('ar-SA')} ر.س` },
            { label: 'واتساب نشط',  val: stats.wa_connected ?? 0 },
          ].map(item => (
            <div key={item.label} className="text-center">
              <p className="text-lg font-black">{item.val}</p>
              <p className="text-slate-400 text-xs">{item.label}</p>
            </div>
          ))}
        </div>
      </div>

    </div>
  )
}
