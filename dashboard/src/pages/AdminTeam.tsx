import { UserCheck } from 'lucide-react'

export default function AdminTeam() {
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
        <p className="text-slate-400 text-sm mt-1">قريباً — إضافة موظفين وتعيين الأدوار</p>
      </div>

      <div className="bg-slate-50 rounded-2xl border border-slate-100 p-5">
        <h2 className="font-bold text-slate-700 text-sm mb-3">الأدوار المخطط لها</h2>
        <div className="space-y-2">
          {[
            { role: 'owner',         label: 'المالك',           desc: 'صلاحيات كاملة على المنصة' },
            { role: 'super_admin',   label: 'مدير عام',         desc: 'إدارة التجار والإيرادات' },
            { role: 'staff',         label: 'موظف دعم',         desc: 'الدخول للحسابات ومساعدة التجار' },
            { role: 'merchant_admin',label: 'مدير متجر',        desc: 'إدارة متجر معين بالكامل' },
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
