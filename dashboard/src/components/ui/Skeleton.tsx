/**
 * Skeleton loading components.
 * Use these as placeholders while API data is fetching.
 *
 * All skeletons use `animate-pulse` with slate-100 background — consistent
 * with the dashboard's white-card surface.
 *
 * Exports:
 *   SkeletonLine      — single text line (configurable width)
 *   SkeletonBlock     — generic rectangle block
 *   SkeletonAvatar    — circular avatar placeholder
 *   SkeletonStatCard  — matches the StatCard layout
 *   SkeletonStatGrid  — 4-up grid of SkeletonStatCards
 *   SkeletonTableRow  — single table row
 *   SkeletonTable     — full table with configurable row count
 *   SkeletonCard      — generic card with text lines
 */

interface SkeletonProps {
  className?: string
}

// ── Primitives ────────────────────────────────────────────────────────────────

export function SkeletonLine({ className = 'w-32' }: SkeletonProps) {
  return (
    <div className={`h-3 bg-slate-100 rounded-full animate-pulse ${className}`} />
  )
}

export function SkeletonBlock({ className = 'w-full h-10' }: SkeletonProps) {
  return (
    <div className={`bg-slate-100 rounded-lg animate-pulse ${className}`} />
  )
}

export function SkeletonAvatar({ className = 'w-8 h-8' }: SkeletonProps) {
  return (
    <div className={`bg-slate-100 rounded-full animate-pulse shrink-0 ${className}`} />
  )
}

// ── StatCard skeleton ─────────────────────────────────────────────────────────

export function SkeletonStatCard() {
  return (
    <div className="card p-5">
      <div className="flex items-start justify-between">
        <div className="flex-1 space-y-2">
          <SkeletonLine className="w-24 h-2.5" />
          <SkeletonLine className="w-32 h-8" />
          <SkeletonLine className="w-20 h-2.5" />
        </div>
        <SkeletonBlock className="w-10 h-10 rounded-xl" />
      </div>
    </div>
  )
}

export function SkeletonStatGrid({ cols = 4 }: { cols?: 2 | 3 | 4 }) {
  const gridClass =
    cols === 2 ? 'grid-cols-1 sm:grid-cols-2' :
    cols === 3 ? 'grid-cols-1 sm:grid-cols-3' :
                 'grid-cols-2 lg:grid-cols-4'

  return (
    <div className={`grid ${gridClass} gap-4`}>
      {Array.from({ length: cols }).map((_, i) => (
        <SkeletonStatCard key={i} />
      ))}
    </div>
  )
}

// ── Table skeletons ───────────────────────────────────────────────────────────

export function SkeletonTableRow({ cols = 5 }: { cols?: number }) {
  const widths = ['w-16', 'w-28', 'w-24', 'w-12', 'w-16', 'w-20', 'w-10']

  return (
    <tr>
      {Array.from({ length: cols }).map((_, i) => (
        <td key={i} className="px-5 py-3.5">
          <SkeletonLine className={widths[i % widths.length]} />
        </td>
      ))}
    </tr>
  )
}

export function SkeletonTable({
  rows = 5,
  cols = 5,
  headers,
}: {
  rows?: number
  cols?: number
  headers?: string[]
}) {
  return (
    <div className="card overflow-hidden">
      {/* Toolbar placeholder */}
      <div className="flex items-center gap-3 px-5 py-4 border-b border-slate-100">
        <SkeletonBlock className="w-48 h-7 rounded-lg" />
        <div className="flex-1" />
        <SkeletonBlock className="w-36 h-7 rounded-lg" />
        <SkeletonBlock className="w-20 h-7 rounded-lg" />
      </div>

      <div className="overflow-x-auto">
        <table className="w-full">
          {/* Header */}
          <thead>
            <tr className="border-b border-slate-100">
              {(headers ?? Array.from({ length: cols }, (_, i) => String(i))).map((h) => (
                <th key={h} className="text-left px-5 py-3">
                  <SkeletonLine className="w-16 h-2.5" />
                </th>
              ))}
            </tr>
          </thead>
          {/* Rows */}
          <tbody className="divide-y divide-slate-100">
            {Array.from({ length: rows }).map((_, i) => (
              <SkeletonTableRow key={i} cols={cols} />
            ))}
          </tbody>
        </table>
      </div>

      {/* Pagination placeholder */}
      <div className="flex items-center justify-between px-5 py-3 border-t border-slate-100">
        <SkeletonLine className="w-32 h-2.5" />
        <div className="flex items-center gap-1">
          <SkeletonBlock className="w-16 h-7 rounded-lg" />
          <SkeletonBlock className="w-7 h-7 rounded-lg" />
          <SkeletonBlock className="w-7 h-7 rounded-lg" />
          <SkeletonBlock className="w-12 h-7 rounded-lg" />
        </div>
      </div>
    </div>
  )
}

// ── Generic card skeleton ─────────────────────────────────────────────────────

export function SkeletonCard({ lines = 3, hasHeader = true }: { lines?: number; hasHeader?: boolean }) {
  return (
    <div className="card overflow-hidden">
      {hasHeader && (
        <div className="flex items-center justify-between px-5 py-4 border-b border-slate-100">
          <SkeletonLine className="w-28 h-3.5" />
          <SkeletonLine className="w-14 h-3" />
        </div>
      )}
      <div className="p-5 space-y-3">
        {Array.from({ length: lines }).map((_, i) => (
          <SkeletonLine
            key={i}
            className={`h-3 ${i % 3 === 0 ? 'w-full' : i % 3 === 1 ? 'w-4/5' : 'w-3/5'}`}
          />
        ))}
      </div>
    </div>
  )
}

// ── Conversation list skeleton ────────────────────────────────────────────────

export function SkeletonConversationList({ rows = 4 }: { rows?: number }) {
  return (
    <div className="space-y-0 divide-y divide-slate-100">
      {Array.from({ length: rows }).map((_, i) => (
        <div key={i} className="flex items-start gap-3 px-4 py-3.5 animate-pulse">
          <SkeletonAvatar className="w-8 h-8 mt-0.5" />
          <div className="flex-1 space-y-2">
            <div className="flex items-center justify-between">
              <SkeletonLine className="w-28 h-3" />
              <SkeletonLine className="w-8 h-2.5" />
            </div>
            <SkeletonLine className="w-48 h-2.5" />
            <SkeletonLine className="w-12 h-4 rounded-full" />
          </div>
        </div>
      ))}
    </div>
  )
}
