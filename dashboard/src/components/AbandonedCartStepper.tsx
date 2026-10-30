import { Check, Clock, AlertCircle, Minus } from 'lucide-react'
import type { AbandonedCartRecoveryStep } from '../api/autopilot'

const statusLabels: Record<string, string> = {
  accepted: 'قُبل لدى المزود', sent: 'قُبل لدى المزود', delivered: 'تم التسليم', read: 'تمت القراءة',
  failed: 'تعذر الإرسال أو التسليم', pending: 'بانتظار التنفيذ', upcoming: 'مرحلة قادمة',
  skipped: 'تم التجاوز أو الإلغاء', unknown: 'النتيجة غير معروفة',
}

export default function AbandonedCartStepper({ steps }: { steps: AbandonedCartRecoveryStep[] }) {
  // Retries remain in the expanded attempt history, rather than inventing
  // additional reminder stages. The API sorts attempts by stage/event id.
  const stages = [...new Map(steps.map(step => [step.step_idx, step])).values()]
  const current = stages.findIndex(step => step.status === 'pending')
  return <div className="rounded-2xl border border-blue-100 bg-white p-5">
    <p className="text-xs text-slate-500 mb-5">العلامة الزرقاء تعني اكتمال إجراء الإرسال. حالة التسليم والقراءة موضحة بالنص لكل مرحلة.</p>
    <ol aria-label="مسار مراحل التذكير" dir="rtl" className="flex flex-col sm:flex-row">
      {stages.map((step, index) => {
        const status = step.delivery_status || step.status
        const failed = status === 'failed' || step.status === 'failed'
        const skipped = step.status === 'skipped'
        const unknown = !['accepted', 'sent', 'delivered', 'read', 'pending', 'upcoming', 'skipped', 'failed'].includes(status)
        const complete = !failed && !skipped && !unknown && ['accepted', 'sent', 'delivered', 'read'].includes(status)
        const active = !complete && !failed && !skipped && !unknown && index === current
        const state = failed ? 'failed' : skipped ? 'skipped' : unknown ? 'unknown' : complete ? 'complete' : active ? 'current' : 'upcoming'
        const circle = failed ? 'bg-red-50 text-red-700 border-red-200'
          : unknown ? 'bg-amber-50 text-amber-700 border-amber-200'
          : complete ? 'bg-blue-600 text-white border-blue-600'
          : active ? 'bg-blue-100 text-blue-800 border-blue-600 ring-4 ring-blue-50'
          : 'bg-slate-100 text-slate-400 border-slate-200'
        return <li key={step.step_idx} data-stage-state={state} aria-current={active ? 'step' : undefined} className="relative flex gap-3 sm:block sm:flex-1 sm:text-center min-w-0">
          {index < stages.length - 1 && <div aria-hidden="true" className={`absolute right-5 top-10 bottom-0 w-0.5 sm:right-1/2 sm:top-5 sm:bottom-auto sm:h-0.5 sm:w-full ${complete ? 'bg-blue-500' : 'bg-slate-200'}`} />}
          <div className={`relative z-10 shrink-0 w-10 h-10 rounded-full border-2 flex items-center justify-center sm:mx-auto ${circle}`}>
            {failed || unknown ? <AlertCircle className="w-5 h-5" /> : complete ? <Check className="w-5 h-5" /> : skipped ? <Minus className="w-5 h-5" /> : active ? <Clock className="w-5 h-5" /> : step.step_idx}
          </div>
          <div className="relative z-10 pb-6 sm:pb-0 sm:pt-3 sm:px-2">
            <h3 className={`font-semibold text-sm ${active ? 'text-blue-900' : 'text-slate-800'}`}>التذكير {step.step_idx}</h3>
            <p className={`text-xs mt-1 ${failed ? 'text-red-700' : 'text-slate-600'}`}>{statusLabels[status] || statusLabels.unknown}</p>
            <p className="text-[11px] text-slate-400 mt-1">{complete && step.sent_at
              ? new Date(step.sent_at).toLocaleString('ar-SA', { timeZone: 'Asia/Riyadh', hour: '2-digit', minute: '2-digit' }) + ' — الرياض'
              : skipped ? 'توقف هذا الإجراء' : 'موعد الإرسال غير معروف'}</p>
          </div>
        </li>
      })}
    </ol>
  </div>
}
