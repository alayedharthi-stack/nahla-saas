import {
  AlertTriangle,
  BookOpen,
  Clock,
  FileEdit,
  Plus,
  Sparkles,
  Zap,
} from 'lucide-react'
import { computeKbPageStats, formatKbDate } from './kbPageStats'
import type { KnowledgeSection } from '../../api/knowledge'

function classNames(...parts: Array<string | false | null | undefined>): string {
  return parts.filter(Boolean).join(' ')
}

interface KbPageHeaderBlockProps {
  sections: KnowledgeSection[]
  onAddInfo: () => void
  onQuickUpdate: () => void
  onOrganize: () => void
  onReviewCenter: () => void
}

export function KbPageHeaderBlock({
  sections,
  onAddInfo,
  onQuickUpdate,
  onOrganize,
  onReviewCenter,
}: KbPageHeaderBlockProps) {
  const stats = computeKbPageStats(sections)

  return (
    <div className="space-y-4">
      <div className="space-y-1">
        <h1 className="text-xl md:text-2xl font-bold text-slate-900 flex items-center gap-2">
          <BookOpen className="w-6 h-6 text-brand-500" />
          قاعدة المعرفة
        </h1>
        <p className="text-sm text-slate-600 leading-relaxed max-w-3xl">
          كل ما تعرفه نحلة عن متجرك وسياساتك، وما يسمح لها باستخدامه في الردود.
        </p>
      </div>

      <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-5 gap-2.5">
        <StatCard label="معلومات نشطة" value={stats.activeCount} tone="green" />
        <StatCard label="مسودات" value={stats.draftCount} tone="blue" />
        <StatCard label="تحتاج مراجعة" value={stats.needsReviewCount} tone="orange" />
        <StatCard label="تحذيرات" value={stats.warningCount} tone="amber" />
        <StatCard
          label="آخر تحديث"
          value={formatKbDate(stats.lastUpdated)}
          tone="slate"
          isDate
        />
      </div>

      <div className="flex flex-wrap gap-2">
        <ActionBtn icon={<Plus className="w-4 h-4" />} label="إضافة معلومة" onClick={onAddInfo} primary />
        <ActionBtn icon={<Zap className="w-4 h-4" />} label="تحديث سريع" onClick={onQuickUpdate} />
        <ActionBtn icon={<Sparkles className="w-4 h-4" />} label="اقتراح تنظيم بالذكاء" onClick={onOrganize} />
        <ActionBtn icon={<FileEdit className="w-4 h-4" />} label="مركز المراجعة" onClick={onReviewCenter} />
      </div>
    </div>
  )
}

function StatCard({
  label,
  value,
  tone,
  isDate,
}: {
  label: string
  value: number | string
  tone: 'green' | 'blue' | 'orange' | 'amber' | 'slate'
  isDate?: boolean
}) {
  const borders = {
    green: 'border-emerald-200 bg-emerald-50/50',
    blue: 'border-sky-200 bg-sky-50/40',
    orange: 'border-orange-200 bg-orange-50/40',
    amber: 'border-amber-200 bg-amber-50/40',
    slate: 'border-slate-200 bg-white',
  }
  const values = {
    green: 'text-emerald-800',
    blue: 'text-sky-800',
    orange: 'text-orange-800',
    amber: 'text-amber-800',
    slate: 'text-slate-800',
  }
  return (
    <div className={classNames('rounded-xl border px-3 py-2.5', borders[tone])}>
      <p className="text-[10px] font-medium text-slate-500 mb-0.5">{label}</p>
      <p
        className={classNames(
          'font-bold tabular-nums',
          isDate ? 'text-xs md:text-sm' : 'text-lg',
          values[tone],
        )}
      >
        {isDate && value !== '—' ? (
          <span className="inline-flex items-center gap-1">
            <Clock className="w-3 h-3 opacity-60" />
            {value}
          </span>
        ) : (
          value
        )}
      </p>
    </div>
  )
}

function ActionBtn({
  icon,
  label,
  onClick,
  primary,
}: {
  icon: React.ReactNode
  label: string
  onClick: () => void
  primary?: boolean
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={classNames(
        'inline-flex items-center gap-1.5 px-3 py-2 rounded-xl text-xs font-semibold transition-colors',
        primary
          ? 'bg-brand-500 hover:bg-brand-600 text-white shadow-sm'
          : 'bg-white border border-slate-200 text-slate-700 hover:bg-slate-50',
      )}
    >
      {icon}
      {label}
    </button>
  )
}

export function KbDoctrineBanner() {
  return (
    <div className="rounded-xl border border-slate-200 bg-slate-50/80 px-4 py-3 text-xs text-slate-700 leading-relaxed flex gap-2">
      <AlertTriangle className="w-4 h-4 text-amber-600 shrink-0 mt-0.5" />
      <span>
        الحقائق التشغيلية لها مصادر محددة: الأسعار والتوفر والأصناف من الكتالوج،
        الشحن والدفع والسياسات من قاعدة المعرفة، وأرقام التواصل من إعدادات التصعيد.
        لا تضع معلومة تشغيلية في غير مصدرها حتى لا تظهر ردود غير دقيقة.
      </span>
    </div>
  )
}
