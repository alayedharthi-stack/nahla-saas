import { useCallback, useState } from 'react'
import {
  AlertTriangle,
  Loader2,
  RefreshCcw,
  Sparkles,
  Wand2,
} from 'lucide-react'
import {
  knowledgeApi,
  type RepairSuggestion,
} from '../../api/knowledge'

function classNames(...parts: Array<string | false | null | undefined>): string {
  return parts.filter(Boolean).join(' ')
}

const REPAIR_KIND_AR: Record<string, string> = {
  move: 'إعادة تصنيف',
  duplicate: 'تكرار',
  contamination: 'خلط سلوك/حقائق',
}

const SEVERITY_AR: Record<string, { label: string; bg: string; text: string }> = {
  critical: { label: 'عالية', bg: 'bg-red-100', text: 'text-red-800' },
  warn: { label: 'متوسطة', bg: 'bg-amber-100', text: 'text-amber-800' },
  info: { label: 'منخفضة', bg: 'bg-slate-100', text: 'text-slate-700' },
}

interface KbRepairPanelProps {
  kindLabelByKind: Map<string, string>
  onOpenSection: (sectionId: number) => void
}

export function KbRepairPanel({ kindLabelByKind, onOpenSection }: KbRepairPanelProps) {
  const [items, setItems] = useState<RepairSuggestion[]>([])
  const [loading, setLoading] = useState(false)
  const [hasRun, setHasRun] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const run = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const res = await knowledgeApi.getRepairPreview()
      setItems(res.suggestions || [])
      setHasRun(true)
    } catch (err) {
      setError((err as Error).message || 'تعذّر تحميل اقتراحات التنظيم')
    } finally {
      setLoading(false)
    }
  }, [])

  return (
    <div className="rounded-xl border border-orange-200/80 bg-orange-50/30 p-4 space-y-3">
      <div className="flex items-start justify-between gap-3 flex-wrap">
        <div className="min-w-0">
          <p className="text-sm font-semibold text-slate-900 flex items-center gap-2">
            <Wand2 className="w-4 h-4 text-orange-600" />
            اقتراح تنظيم بالذكاء
          </p>
          <p className="text-xs text-slate-600 mt-0.5 leading-relaxed">
            فحص هيكلي بدون GPT — يقترح إعادة تصنيف أو دمج. لا يُطبَّق تلقائياً.
          </p>
        </div>
        <button
          type="button"
          onClick={run}
          disabled={loading}
          className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-orange-500 hover:bg-orange-600 text-white text-xs font-semibold disabled:opacity-50"
        >
          {loading ? (
            <Loader2 className="w-3.5 h-3.5 animate-spin" />
          ) : (
            <Sparkles className="w-3.5 h-3.5" />
          )}
          {hasRun ? 'إعادة الفحص' : 'فحص التنظيم'}
        </button>
      </div>

      {error && (
        <p className="text-xs text-red-600 flex items-center gap-1">
          <AlertTriangle className="w-3.5 h-3.5" /> {error}
        </p>
      )}

      {hasRun && !loading && items.length === 0 && (
        <p className="text-xs text-emerald-700 bg-emerald-50 border border-emerald-100 rounded-lg px-3 py-2">
          لا توجد مشاكل هيكلية واضحة — التصنيف يبدو متسقاً.
        </p>
      )}

      {items.length > 0 && (
        <ul className="space-y-2">
          {items.map((s, idx) => {
            const sev = SEVERITY_AR[s.severity] || SEVERITY_AR.info
            const sid = s.section_ids[0]
            return (
              <li
                key={`${s.kind}-${sid}-${idx}`}
                className="rounded-lg border border-white bg-white/90 p-3 text-xs space-y-1.5"
              >
                <div className="flex flex-wrap gap-1.5 items-center">
                  <span className="font-semibold text-slate-800">
                    {REPAIR_KIND_AR[s.kind] || s.kind}
                  </span>
                  <span className={classNames('px-1.5 py-0.5 rounded font-semibold', sev.bg, sev.text)}>
                    {sev.label}
                  </span>
                  {s.suggested_kind && (
                    <span className="text-slate-500">
                      → {kindLabelByKind.get(s.suggested_kind) || s.suggested_kind}
                    </span>
                  )}
                </div>
                <p className="text-slate-600 leading-relaxed">{s.reason_ar}</p>
                {sid != null && (
                  <button
                    type="button"
                    onClick={() => onOpenSection(sid)}
                    className="text-brand-600 font-medium hover:underline"
                  >
                    فتح القسم #{sid}
                  </button>
                )}
              </li>
            )
          })}
        </ul>
      )}
    </div>
  )
}

interface KbReviewCenterProps {
  kindLabelByKind: Map<string, string>
  onOpenSection: (sectionId: number) => void
  improvementSlot: React.ReactNode
}

/** Wraps improvement suggestions + repair panel under "مركز المراجعة". */
export function KbReviewCenter({
  kindLabelByKind,
  onOpenSection,
  improvementSlot,
}: KbReviewCenterProps) {
  return (
    <div id="kb-review-center" className="card overflow-hidden border-slate-200">
      <div className="px-5 py-4 border-b border-slate-100 bg-slate-50/50">
        <h2 className="text-base font-bold text-slate-900">المراجعة والتحذيرات</h2>
        <p className="text-xs text-slate-600 mt-0.5">
          اقتراحات التحسين والتنظيم — معاينة فقط، بدون تطبيق تلقائي.
        </p>
      </div>
      <div className="p-5 space-y-5">
        {improvementSlot}
        <KbRepairPanel kindLabelByKind={kindLabelByKind} onOpenSection={onOpenSection} />
      </div>
    </div>
  )
}
