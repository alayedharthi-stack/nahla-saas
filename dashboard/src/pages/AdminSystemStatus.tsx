import { useEffect, useState } from 'react'
import { Activity, AlertTriangle, CheckCircle2, Server } from 'lucide-react'
import { adminApi, type AdminSystemEvent, type AdminSystemHealth } from '../api/admin'

export default function AdminSystemStatus() {
  const [health, setHealth] = useState<AdminSystemHealth | null>(null)
  const [events, setEvents] = useState<AdminSystemEvent[]>([])
  const [isolation, setIsolation] = useState<{ all_checks_passed: boolean; issues: string[] } | null>(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    Promise.all([
      adminApi.systemHealth(),
      adminApi.systemEvents({ limit: 50 }),
      adminApi.tenantIsolation(),
    ]).then(([healthData, eventData, isolationData]) => {
      setHealth(healthData)
      setEvents(eventData.events)
      setIsolation({
        all_checks_passed: isolationData.all_checks_passed,
        issues: isolationData.issues,
      })
    }).finally(() => setLoading(false))
  }, [])

  if (loading) {
    return (
      <div className="flex items-center justify-center h-64">
        <div className="animate-spin rounded-full h-10 w-10 border-b-2 border-amber-500" />
      </div>
    )
  }

  return (
    <div className="p-6 space-y-6" dir="rtl">
      <div className="flex items-center gap-3">
        <div className="w-10 h-10 rounded-xl bg-slate-800 flex items-center justify-center shadow-lg shadow-slate-800/20">
          <Activity className="w-5 h-5 text-white" />
        </div>
        <div>
          <h1 className="text-lg font-black text-slate-800">حالة النظام</h1>
          <p className="text-slate-400 text-xs">مراقبة صحة المنصة والأحداث وعزل المستأجرين</p>
        </div>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
        <div className="bg-white rounded-2xl border border-slate-100 p-5 shadow-sm">
          <div className="flex items-center justify-between mb-2">
            <span className="text-slate-500 text-xs font-medium">الحالة العامة</span>
            <Server className="w-4 h-4 text-slate-500" />
          </div>
          <p className="text-2xl font-black text-slate-800">{health?.status || 'unknown'}</p>
        </div>
        <div className="bg-white rounded-2xl border border-slate-100 p-5 shadow-sm">
          <div className="flex items-center justify-between mb-2">
            <span className="text-slate-500 text-xs font-medium">Tenant Isolation</span>
            {isolation?.all_checks_passed ? <CheckCircle2 className="w-4 h-4 text-green-500" /> : <AlertTriangle className="w-4 h-4 text-amber-500" />}
          </div>
          <p className="text-sm font-bold text-slate-800">{isolation?.all_checks_passed ? 'سليم' : 'يحتاج مراجعة'}</p>
        </div>
        <div className="bg-white rounded-2xl border border-slate-100 p-5 shadow-sm">
          <div className="flex items-center justify-between mb-2">
            <span className="text-slate-500 text-xs font-medium">الأحداث المعروضة</span>
            <Activity className="w-4 h-4 text-indigo-500" />
          </div>
          <p className="text-2xl font-black text-slate-800">{events.length.toLocaleString('ar-SA')}</p>
        </div>
      </div>

      <div className="bg-white rounded-2xl border border-slate-100 shadow-sm p-5">
        <h2 className="font-bold text-slate-700 text-sm mb-3">المكوّنات</h2>
        <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
          {Object.entries(health?.components ?? {}).map(([name, component]) => (
            <div key={name} className="rounded-xl border border-slate-100 p-4 bg-slate-50">
              <p className="font-semibold text-slate-700">{name}</p>
              <p className="text-xs text-slate-500 mt-1">{JSON.stringify(component)}</p>
            </div>
          ))}
        </div>
      </div>

      <div className="bg-white rounded-2xl border border-slate-100 shadow-sm p-5">
        <h2 className="font-bold text-slate-700 text-sm mb-3">نتيجة فحص العزل</h2>
        {isolation?.issues?.length ? (
          <div className="space-y-2">
            {isolation.issues.map(issue => (
              <div key={issue} className="text-sm text-amber-700 bg-amber-50 border border-amber-100 rounded-xl px-3 py-2">{issue}</div>
            ))}
          </div>
        ) : (
          <div className="text-sm text-green-700 bg-green-50 border border-green-100 rounded-xl px-3 py-2">لا توجد مشاكل عزل مرصودة.</div>
        )}
      </div>

      <div className="bg-white rounded-2xl border border-slate-100 shadow-sm overflow-hidden">
        <div className="px-5 py-4 border-b border-slate-50">
          <h2 className="font-bold text-slate-700 text-sm">أحدث الأحداث</h2>
        </div>
        <div className="divide-y divide-slate-50">
          {events.map(event => (
            <div key={event.id} className="px-5 py-3">
              <p className="text-xs text-slate-400">{event.tenant_name} · {event.category} · {event.event_type}</p>
              <p className="text-sm text-slate-700">{event.summary || '—'}</p>
            </div>
          ))}
        </div>
      </div>
    </div>
  )
}
