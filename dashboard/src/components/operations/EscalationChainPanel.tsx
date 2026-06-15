import {
  ArrowDown, ChevronDown, ChevronUp, Info, Pencil, Phone, Plus, Trash2,
} from 'lucide-react'
import Badge from '../ui/Badge'
import type { BranchEscalationStep } from '../../api/operationsCenter'
import {
  ESCALATION_CHAIN_TYPES,
  ESCALATION_EXAMPLE_LEVELS,
  type EscalationChainType,
} from '../../lib/escalationTypes'

interface EscalationChainPanelProps {
  steps: BranchEscalationStep[]
  chainType: EscalationChainType
  error?: string
  reordering?: boolean
  onChainTypeChange: (type: EscalationChainType) => void
  onAdd: () => void
  onEdit: (step: BranchEscalationStep) => void
  onDelete: (step: BranchEscalationStep) => void
  onMove: (index: number, direction: -1 | 1) => void
}

export default function EscalationChainPanel({
  steps,
  chainType,
  error,
  reordering = false,
  onChainTypeChange,
  onAdd,
  onEdit,
  onDelete,
  onMove,
}: EscalationChainPanelProps) {
  return (
    <div className="space-y-5">
      <div className="rounded-xl border border-brand-100 bg-brand-50/60 px-4 py-3 flex gap-3">
        <Info className="w-5 h-5 text-brand-600 shrink-0 mt-0.5" />
        <p className="text-sm text-slate-700 leading-relaxed">
          عند طلب العميل التواصل مع الفرع أو عند عدم استجابة المستوى الحالي، ينتقل النظام
          تلقائياً إلى المستوى التالي حسب ترتيب التصعيد أدناه.
        </p>
      </div>

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
            {steps.length === 0
              ? 'ابدأ بإضافة المستوى الأول — يُتصل به العميل أولاً'
              : `${steps.length} مستوى — يبدأ من المستوى 1 وينتقل للتالي عند الحاجة`}
          </p>
        </div>
        <button type="button" className="btn-primary flex items-center gap-2 shrink-0" onClick={onAdd}>
          <Plus className="w-4 h-4" />
          إضافة مستوى
        </button>
      </div>

      {steps.length === 0 ? (
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
            اضغط «إضافة مستوى» لبناء سلسلة التصعيد لهذا الفرع.
          </p>
        </div>
      ) : (
        <div className="relative space-y-0">
          {steps.map((step, index) => (
            <div key={step.id} className="relative flex gap-4 pb-6 last:pb-0">
              {index < steps.length - 1 && (
                <div
                  className="absolute top-10 bottom-0 w-0.5 bg-brand-200"
                  style={{ right: '1.1875rem' }}
                  aria-hidden
                />
              )}

              <div className="relative z-10 shrink-0">
                <div className="w-10 h-10 rounded-full bg-brand-600 text-white flex items-center justify-center font-bold text-sm shadow-sm">
                  {step.escalation_level}
                </div>
              </div>

              <div className="flex-1 min-w-0 card p-4">
                <div className="flex flex-wrap items-start justify-between gap-3">
                  <div className="min-w-0 space-y-1">
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="font-semibold text-slate-900">{step.display_name}</span>
                      <Badge label={`المستوى ${step.escalation_level}`} variant="slate" />
                      {!step.is_active && (
                        <Badge label="معطّل" variant="slate" />
                      )}
                    </div>
                    {step.role && (
                      <p className="text-sm text-slate-600">{step.role}</p>
                    )}
                    <p className="text-sm text-slate-500 flex items-center gap-1.5 font-mono" dir="ltr">
                      <Phone className="w-3.5 h-3.5 shrink-0" />
                      {step.phone_e164}
                    </p>
                  </div>

                  <div className="flex flex-wrap items-center gap-1">
                    <button
                      type="button"
                      className="inline-flex items-center gap-1 px-2 py-1.5 rounded-lg text-xs text-slate-600 hover:bg-slate-100"
                      title="تعديل"
                      onClick={() => onEdit(step)}
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
                      disabled={index === steps.length - 1 || reordering}
                      onClick={() => onMove(index, 1)}
                    >
                      <ChevronDown className="w-3.5 h-3.5" />
                      خفض
                    </button>
                    <button
                      type="button"
                      className="inline-flex items-center gap-1 px-2 py-1.5 rounded-lg text-xs text-red-600 hover:bg-red-50"
                      title="حذف"
                      onClick={() => onDelete(step)}
                    >
                      <Trash2 className="w-3.5 h-3.5" />
                      حذف
                    </button>
                  </div>
                </div>

                {index < steps.length - 1 && (
                  <div className="mt-3 pt-3 border-t border-slate-100 flex items-center gap-1.5 text-xs text-brand-600">
                    <ArrowDown className="w-3.5 h-3.5" />
                    <span>عند عدم الاستجابة → المستوى {steps[index + 1].escalation_level}</span>
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
