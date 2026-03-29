type Variant = 'green' | 'amber' | 'red' | 'blue' | 'slate' | 'purple'

interface BadgeProps {
  label: string
  variant?: Variant
  dot?: boolean
}

const styles: Record<Variant, string> = {
  green:  'bg-emerald-50 text-emerald-700 ring-emerald-600/20',
  amber:  'bg-amber-50  text-amber-700  ring-amber-600/20',
  red:    'bg-red-50    text-red-700    ring-red-600/20',
  blue:   'bg-blue-50   text-blue-700   ring-blue-600/20',
  slate:  'bg-slate-100 text-slate-600  ring-slate-500/20',
  purple: 'bg-purple-50 text-purple-700 ring-purple-600/20',
}

const dotStyles: Record<Variant, string> = {
  green:  'bg-emerald-500',
  amber:  'bg-amber-500',
  red:    'bg-red-500',
  blue:   'bg-blue-500',
  slate:  'bg-slate-400',
  purple: 'bg-purple-500',
}

export default function Badge({ label, variant = 'slate', dot = false }: BadgeProps) {
  return (
    <span className={`inline-flex items-center gap-1.5 px-2 py-0.5 rounded-full text-xs font-medium ring-1 ring-inset ${styles[variant]}`}>
      {dot && <span className={`w-1.5 h-1.5 rounded-full ${dotStyles[variant]}`} />}
      {label}
    </span>
  )
}
