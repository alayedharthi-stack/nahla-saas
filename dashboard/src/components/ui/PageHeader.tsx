import { ReactNode } from 'react'

interface PageHeaderProps {
  title: string
  subtitle?: string
  action?: ReactNode
}

/**
 * In-page section header for all main dashboard pages.
 * Provides consistent visual weight, spacing, and a right-aligned action slot.
 *
 * Usage:
 *   <PageHeader
 *     title="Orders"
 *     subtitle="AI-created and manual orders"
 *     action={<button className="btn-primary text-sm"><Plus /> New Order</button>}
 *   />
 */
export default function PageHeader({ title, subtitle, action }: PageHeaderProps) {
  return (
    <div className="flex items-start justify-between mb-6">
      <div>
        <h1 className="text-xl font-bold text-slate-900 tracking-tight">{title}</h1>
        {subtitle && (
          <p className="text-sm text-slate-500 mt-0.5 font-normal">{subtitle}</p>
        )}
      </div>
      {action && (
        <div className="shrink-0 ms-4">{action}</div>
      )}
    </div>
  )
}
