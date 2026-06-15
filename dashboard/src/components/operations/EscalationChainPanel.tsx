import {
  ArrowDown, ChevronDown, ChevronUp, Info, Pencil, Phone, Plus, Trash2, Users,
} from 'lucide-react'
import type { BranchContact, EscalationLevel } from '../../api/operationsCenter'
import {
  ESCALATION_CHAIN_TYPES,
  ESCALATION_EXAMPLE_LEVELS,
  type EscalationChainType,
} from '../../lib/escalationTypes'

interface EscalationChainPanelProps {
  levels: EscalationLevel[]
  contacts: BranchContact[]
  chainType: EscalationChainType
  error?: string
  reordering?: boolean
  onChainTypeChange: (type: EscalationChainType) => void
  onAdd: () => void
  onEdit: (level: EscalationLevel) => void
  onDelete: (level: EscalationLevel) => void
  onMove: (index: number, direction: -1 | 1) => void
}

export default function EscalationChainPanel({
  levels,
  contacts,
  chainType,
  error,
  reordering = false,
  onChainTypeChange,
  onAdd,
  onEdit,
  onDelete,
  onMove,
}: EscalationChainPanelProps) {
  const activeContactCount = contacts.filter(c => c.is_active).length

  return (
    <div className="space-y-5">
      <div className="rounded-xl border border-brand-100 bg-brand-50/60 px-4 py-3 flex gap-3">
        <Info className="w-5 h-5 text-brand-600 shrink-0 mt-0.5" />
        <p className="text-sm text-slate-700 leading-relaxed">
          عند طلب العميل التواصل مع الفرع أو عند عدم استجابة المستوى الحالي، ينتقل النظام
          تلقائياً إلى المستوى التالي حسب ترتيب التصعيد أدناه. الموظفون يُضافون في تبويب
          «جهات التواصل» — هنا تختار من هم فقط.
        </p>
      </div>

      {activeContactCount === 0 && (
        <div className="rounded-xl border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-amber-900">
          أضف جهات التواصل أولاً من التبويب المجاور، ثم ارجع لبناء سلسلة التصعيد.
        </div>
      )}

      <div className="card p-4 space-y-2">
        <label className="block text-sm font-medium text-slate-700">نوع التصعيد</label>
        <p className="text-xs text-slate-500">
          حالياً تُستخدم سلسلة واحدة من نوع «عام». الأنواع الأخرى ستُفعَّل لاحقاً لسلاسل منفصلة.
        </p>
        <div className="flex flex-wrap gap-2 pt-1">
          {ESCALATION_CHAIN_TYPES.map(({ id, label, available }) => {
            const selected = chainType === id
            return (
              <button
                key={id}
                type="button"
                disabled={!available}
                onClick={() => available && onChainTypeChange(id)}
                className={`inline-flex items-center gap-1.5 px-3 py-1.5 rounded-full text-sm border transition-colors ${
                  selected
                    ? 'border-brand-500 bg-brand-50 text-brand-700 font-medium'
                    : available
                      ? 'border-slate-200 bg-white text-slate-600 hover:border-slate-300'
                      : 'border-slate-100 bg-slate-50 text-slate-400 cursor-not-allowed'
                }`}
              >
                {label}
                {!available && (
                  <span className="text-[10px] px-1.5 py-0.5 rounded bg-slate-200 text-slate-500">
                    قريباً
                  </span>
                )}
              </button>
            )
          })}
        </div>
      </div>

      {error && (
        <div className="rounded-xl border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700">
          {error}
        </div>
      )}

      <div className="flex items-center justify-between gap-3">
        <div>
          <h3 className="font-semibold text-slate-900">سلسلة التصعيد</h3>
          <p className="text-xs text-slate-500 mt-0.5">
            {levels.length === 0
              ? 'ابدأ بإضافة المستوى الأول — يُتصل به العميل أولاً'
              : `${levels.length} مستوى — يبدأ من المستوى 1 وينتقل للتالي عند الحاجة`}
          </p>
        </div>
        <button
          type="button"
          className="btn-primary flex items-center gap-2 shrink-0"
          onClick={onAdd}
          disabled={activeContactCount === 0}
        >
          <Plus className="w-4 h-4" />
          إضافة مستوى
        </button>
      </div>

      {levels.length === 0 ? (
        <div className="space-y-4">
          <div className="card p-5 border-dashed border-slate-200 bg-slate-50/50">
            <p className="text-sm font-medium text-slate-700 mb-3">مثال على سلسلة تصعيد</p>
            <div className="space-y-2">
              {ESCALATION_EXAMPLE_LEVELS.map(({ level, role }) => (
                <div
                  key={level}
                  className="flex items-center gap-3 text-sm text-slate-500"
                >
                  <span className="w-8 h-8 rounded-full bg-slate-200/80 text-slate-600 flex items-center justify-center text-xs font-bold shrink-0">
                    {level}
                  </span>
                  <span>
                    المستوى {level}: <span className="text-slate-600">{role}</span>
                  </span>
                </div>
              ))}
            </div>
          </div>
          <p className="text-center text-sm text-slate-500">
            اضغط «إضافة مستوى» واختر من جهات التواصل الموجودة.
          </p>
        </div>
      ) : (
        <div className="relative space-y-0">
          {levels.map((level, index) => (
            <div key={level.escalation_level} className="relative flex gap-4 pb-6 last:pb-0">
              {index < levels.length - 1 && (
                <div
                  className="absolute top-10 bottom-0 w-0.5 bg-brand-200"
                  style={{ right: '1.1875rem' }}
                  aria-hidden
                />
              )}

              <div className="relative z-10 shrink-0">
                <div className="w-10 h-10 rounded-full bg-brand-600 text-white flex items-center justify-center font-bold text-sm shadow-sm">
                  {level.escalation_level}
                </div>
              </div>

              <div className="flex-1 min-w-0 card p-4">
                <div className="flex flex-wrap items-start justify-between gap-3">
                  <div className="min-w-0 space-y-2 flex-1">
                    <div className="flex items-center gap-2 text-xs text-slate-500">
                      <Users className="w-3.5 h-3.5" />
                      <span>
                        {level.contacts.length === 1
                          ? 'موظف واحد'
                          : `${level.contacts.length} موظفين`}
                      </span>
                    </div>
                    {level.contacts.map(contact => (
                      <div key={contact.id} className="border-r-2 border-brand-200 pr-3">
                        <div className="font-semibold text-slate-900">{contact.display_name}</div>
                        {contact.role && (
                          <p className="text-sm text-slate-600">{contact.role}</p>
                        )}
                        <p className="text-sm text-slate-500 flex items-center gap-1.5 font-mono" dir="ltr">
                          <Phone className="w-3.5 h-3.5 shrink-0" />
                          {contact.phone_e164}
                        </p>
                      </div>
                    ))}
                  </div>

                  <div className="flex flex-wrap items-center gap-1">
                    <button
                      type="button"
                      className="inline-flex items-center gap-1 px-2 py-1.5 rounded-lg text-xs text-slate-600 hover:bg-slate-100"
                      title="تعديل"
                      onClick={() => onEdit(level)}
                    >
                      <Pencil className="w-3.5 h-3.5" />
                      تعديل
                    </button>
                    <button
                      type="button"
                      className="inline-flex items-center gap-1 px-2 py-1.5 rounded-lg text-xs text-slate-600 hover:bg-slate-100 disabled:opacity-40"
                      title="رفع مستوى"
                      disabled={index === 0 || reordering}
                      onClick={() => onMove(index, -1)}
                    >
                      <ChevronUp className="w-3.5 h-3.5" />
                      رفع
                    </button>
                    <button
                      type="button"
                      className="inline-flex items-center gap-1 px-2 py-1.5 rounded-lg text-xs text-slate-600 hover:bg-slate-100 disabled:opacity-40"
                      title="خفض مستوى"
                      disabled={index === levels.length - 1 || reordering}
                      onClick={() => onMove(index, 1)}
                    >
                      <ChevronDown className="w-3.5 h-3.5" />
                      خفض
                    </button>
                    <button
                      type="button"
                      className="inline-flex items-center gap-1 px-2 py-1.5 rounded-lg text-xs text-red-600 hover:bg-red-50"
                      title="حذف"
                      onClick={() => onDelete(level)}
                    >
                      <Trash2 className="w-3.5 h-3.5" />
                      حذف
                    </button>
                  </div>
                </div>

                {index < levels.length - 1 && (
                  <div className="mt-3 pt-3 border-t border-slate-100 flex items-center gap-1.5 text-xs text-brand-600">
                    <ArrowDown className="w-3.5 h-3.5" />
                    <span>عند عدم الاستجابة → المستوى {levels[index + 1].escalation_level}</span>
                  </div>
                )}
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
