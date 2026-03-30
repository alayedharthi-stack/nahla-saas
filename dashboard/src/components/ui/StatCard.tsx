import { LucideIcon, TrendingUp, TrendingDown } from 'lucide-react'

interface StatCardProps {
  label: string
  value: string
  change?: number      // percentage change, positive = up, negative = down
  changeLabel?: string
  icon: LucideIcon
  iconColor?: string
  iconBg?: string
}

export default function StatCard({
  label,
  value,
  change,
  changeLabel = 'مقارنة بالأمس',
  icon: Icon,
  iconColor = 'text-brand-500',
  iconBg = 'bg-brand-50',
}: StatCardProps) {
  const isPositive = change !== undefined && change >= 0

  return (
    <div className="card p-5">
      <div className="flex items-start justify-between">
        <div>
          <p className="text-xs font-medium text-slate-500 uppercase tracking-wide">{label}</p>
          <p className="text-3xl font-bold text-slate-900 mt-1 tracking-tight">{value}</p>
          {change !== undefined && (
            <div className="flex items-center gap-1 mt-2">
              {isPositive ? (
                <TrendingUp className="w-3.5 h-3.5 text-emerald-500" />
              ) : (
                <TrendingDown className="w-3.5 h-3.5 text-red-500" />
              )}
              <span className={`text-xs font-medium ${isPositive ? 'text-emerald-600' : 'text-red-600'}`}>
                {isPositive ? '+' : ''}{change}%
              </span>
              <span className="text-xs text-slate-400">{changeLabel}</span>
            </div>
          )}
        </div>
        <div className={`w-10 h-10 ${iconBg} rounded-xl flex items-center justify-center shrink-0`}>
          <Icon className={`w-5 h-5 ${iconColor}`} />
        </div>
      </div>
    </div>
  )
}
