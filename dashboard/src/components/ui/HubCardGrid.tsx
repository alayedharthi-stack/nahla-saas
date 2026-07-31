import { Link } from 'react-router-dom'
import { ChevronRight, type LucideIcon } from 'lucide-react'

export interface HubCardItem {
  to: string
  icon: LucideIcon
  title: string
  description: string
  isAI?: boolean
}

export function HubSectionHeading({
  title,
  description,
}: {
  title: string
  description?: string
}) {
  return (
    <div className="mb-3">
      <h2 className="text-sm font-semibold text-slate-900">{title}</h2>
      {description && (
        <p className="text-xs text-slate-500 mt-0.5 leading-relaxed">{description}</p>
      )}
    </div>
  )
}

export function HubCardGrid({ items }: { items: HubCardItem[] }) {
  return (
    <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4">
      {items.map(({ to, icon: Icon, title, description, isAI }) => (
        <Link
          key={to}
          to={to}
          className="card p-5 hover:border-brand-200 hover:shadow-sm transition-all group"
        >
          <div className="flex items-start gap-3">
            <div className="w-10 h-10 rounded-xl bg-slate-100 flex items-center justify-center shrink-0 relative">
              <Icon className="w-5 h-5 text-slate-600" />
              {isAI && (
                <span className="absolute -bottom-1 -end-1 inline-flex items-center px-1 py-px rounded bg-amber-500/15 border border-amber-500/50">
                  <span className="text-[6px] font-black text-amber-600 leading-none">AI</span>
                </span>
              )}
            </div>
            <div className="flex-1 min-w-0">
              <div className="flex items-center justify-between gap-2">
                <h3 className="text-sm font-semibold text-slate-900 group-hover:text-brand-600 transition-colors">
                  {title}
                </h3>
                <ChevronRight className="w-4 h-4 text-slate-300 group-hover:text-brand-400 shrink-0" />
              </div>
              <p className="text-xs text-slate-500 mt-1 leading-relaxed">{description}</p>
            </div>
          </div>
        </Link>
      ))}
    </div>
  )
}
