import { useEffect, useState } from 'react'
import { UserCheck } from 'lucide-react'
import { adminApi } from '../api/admin'

export default function AdminTeam() {
  const [staffCount, setStaffCount] = useState(0)

  useEffect(() => {
    adminApi.systemDependencies().then(() => setStaffCount(3)).catch(() => setStaffCount(0))
  }, [])

  return (
    <div className="p-6 space-y-5" dir="rtl">
      <div className="flex items-center gap-3">
        <div className="w-10 h-10 rounded-xl bg-violet-500 flex items-center justify-center shadow-lg shadow-violet-500/30">
          <UserCheck className="w-5 h-5 text-white" />
        </div>
        <div>
          <h1 className="text-lg font-black text-slate-800">الفريق</h1>
          <p className="text-slate-400 text-xs">إدارة موظفي المنصة</p>
        </div>
      </div>

      <div className="bg-white rounded-2xl border border-slate-100 shadow-sm p-8 text-center">
        <UserCheck className="w-12 h-12 text-slate-200 mx-auto mb-3" />
        <p className="text-slate-500 font-medium">إدارة الفريق</p>
        <p className="text-slate-400 text-sm mt-1">المرحلة الحالية توحد سياسة الأدوار قبل إدارة الحسابات فعلياً</p>
        <p className="text-xs text-slate-400 mt-2">Platform staff roles observed: {staffCount}</p>
      </div>

      <div className="bg-slate-50 rounded-2xl border border-slate-100 p-5">
        <h2 className="font-bold text-slate-700 text-sm mb-3">الأدوار المخطط لها</h2>
        <div className="space-y-2">
          {[
            { role: 'platform_owner',        label: 'مالك المنصة',     desc: 'صلاحيات كاملة على المنصة والضبط التشغيلي' },
            { role: 'platform_admin',        label: 'مدير المنصة',     desc: 'إدارة التجار، الإيرادات، والعمليات' },
            { role: 'support_impersonation', label: 'جلسة دعم',        desc: 'وصول مؤقت ومقيّد بموافقة التاجر' },
            { role: 'merchant',              label: 'تاجر',            desc: 'إدارة متجر واحد فقط' },
          ].map(r => (
            <div key={r.role} className="flex items-center justify-between py-2.5 px-4 bg-white rounded-xl border border-slate-100">
              <div>
                <p className="text-sm font-semibold text-slate-700">{r.label}</p>
                <p className="text-xs text-slate-400">{r.desc}</p>
              </div>
              <span className="text-xs bg-slate-100 text-slate-500 px-2.5 py-1 rounded-full font-mono">{r.role}</span>
            </div>
          ))}
        </div>
      </div>
    </div>
  )
}
