import { Eye, X, Bot, AlertTriangle } from 'lucide-react'
import type { KnowledgeSection } from '../../api/knowledge'
import { buildAiPreviewVerdict } from './kbAiPreview'
import { sectionHasSensitiveOperational } from './kbHeuristics'

function classNames(...parts: Array<string | false | null | undefined>): string {
  return parts.filter(Boolean).join(' ')
}

const CHANNEL_LABEL_AR: Record<string, string> = {
  facts: 'معلومات المتجر',
  behavior: 'سلوك المساعد',
  metadata_consumer: 'بيانات منظمة',
}

interface KbAiPreviewModalProps {
  section: KnowledgeSection | null
  onClose: () => void
}

export function KbAiPreviewModal({ section, onClose }: KbAiPreviewModalProps) {
  if (!section) return null

  const verdict = buildAiPreviewVerdict(section)
  const sensitive = sectionHasSensitiveOperational(section)
  const channelLabel = CHANNEL_LABEL_AR[verdict.channel]

  return (
    <div className="fixed inset-0 z-50 bg-black/40 flex items-end sm:items-center justify-center p-2 sm:p-6">
      <div className="bg-white rounded-2xl w-full max-w-lg max-h-[90vh] flex flex-col shadow-xl">
        <div className="px-5 py-3.5 border-b border-slate-100 flex items-center justify-between gap-2">
          <div className="flex items-center gap-2 min-w-0">
            <Bot className="w-4 h-4 text-brand-500 shrink-0" />
            <h3 className="text-sm font-semibold text-slate-900 truncate">
              معاينة كما تراها نحلة
            </h3>
          </div>
          <button type="button" onClick={onClose} className="text-slate-400 hover:text-slate-700">
            <X className="w-4 h-4" />
          </button>
        </div>

        <div className="flex-1 overflow-y-auto p-5 space-y-4 text-sm">
          <div>
            <p className="text-xs text-slate-500 mb-0.5">القسم</p>
            <p className="font-semibold text-slate-900">{section.title || section.kind}</p>
          </div>

          <div className="flex flex-wrap gap-2">
            <StatusPill
              label={verdict.active ? 'نشط' : 'غير نشط'}
              tone={verdict.active ? 'green' : 'gray'}
            />
            <StatusPill
              label={verdict.inPrompt ? 'قابل للاستخدام في الردود' : 'غير مستخدم في الردود'}
              tone={verdict.inPrompt ? 'blue' : 'gray'}
            />
            {channelLabel && (
              <StatusPill label={channelLabel} tone="purple" />
            )}
            {sensitive && (
              <StatusPill label="حقيقة تشغيلية حساسة" tone="amber" />
            )}
          </div>

          {verdict.promptGroupLabel && (
            <p className="text-xs text-slate-600">
              <span className="font-medium text-slate-800">موضع العرض: </span>
              {verdict.promptGroupLabel}
            </p>
          )}

          <ul className="space-y-2">
            {verdict.messages.map((msg, i) => (
              <li
                key={i}
                className="text-xs text-slate-700 leading-relaxed flex gap-2 items-start"
              >
                <Eye className="w-3.5 h-3.5 text-brand-500 shrink-0 mt-0.5" />
                <span>{msg}</span>
              </li>
            ))}
          </ul>

          {sensitive && (
            <div className="rounded-lg border border-amber-200 bg-amber-50/80 px-3 py-2 text-xs text-amber-900 flex gap-2">
              <AlertTriangle className="w-4 h-4 shrink-0" />
              <span>
                أرقام الدفع والتواصل حقائق تشغيلية — يجب أن تطابق الواقع. نحلة لا
                تخترع أرقاماً غير مهيأة.
              </span>
            </div>
          )}

          <p className="text-[10px] text-slate-400 leading-relaxed border-t border-slate-100 pt-3">
            معاينة تعليمية — تعكس ما قد تستخدمه نحلة في الردود، ولا تغيّر سلوك متجرك.
          </p>
        </div>

        <div className="px-5 py-3 border-t border-slate-100">
          <button
            type="button"
            onClick={onClose}
            className="w-full py-2 rounded-lg bg-slate-100 hover:bg-slate-200 text-slate-800 text-sm font-medium"
          >
            إغلاق
          </button>
        </div>
      </div>
    </div>
  )
}

function StatusPill({
  label,
  tone,
}: {
  label: string
  tone: 'green' | 'gray' | 'blue' | 'purple' | 'amber'
}) {
  const styles = {
    green: 'bg-emerald-50 text-emerald-800 border-emerald-200',
    gray: 'bg-slate-100 text-slate-600 border-slate-200',
    blue: 'bg-sky-50 text-sky-800 border-sky-200',
    purple: 'bg-purple-50 text-purple-800 border-purple-200',
    amber: 'bg-amber-50 text-amber-800 border-amber-200',
  }
  return (
    <span
      className={classNames(
        'inline-flex px-2 py-0.5 rounded-full text-[10px] font-semibold border',
        styles[tone],
      )}
    >
      {label}
    </span>
  )
}

/** Inline trigger for section cards. */
export function KbAiPreviewButton({
  section,
  onPreview,
}: {
  section: KnowledgeSection
  onPreview: (s: KnowledgeSection) => void
}) {
  return (
    <button
      type="button"
      title="معاينة كما تراها نحلة"
      onClick={() => onPreview(section)}
      className="p-1.5 rounded hover:bg-brand-50 text-brand-600"
    >
      <Eye className="w-3.5 h-3.5" />
    </button>
  )
}
