/** Dir-aware toggle — knob slides to inline-end when checked via flex justify. */
interface ToggleSwitchProps {
  checked: boolean
  disabled?: boolean
  onClick: () => void
  activeClass?: string
  inactiveClass?: string
  'aria-label'?: string
}

export default function ToggleSwitch({
  checked,
  disabled,
  onClick,
  activeClass = 'bg-emerald-500',
  inactiveClass = 'bg-slate-200',
  'aria-label': ariaLabel,
}: ToggleSwitchProps) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={ariaLabel}
      onClick={onClick}
      disabled={disabled}
      className={`mt-0.5 inline-flex h-5 w-9 shrink-0 items-center rounded-full p-0.5 transition-colors ${
        checked ? activeClass : inactiveClass
      } ${checked ? 'justify-end' : 'justify-start'} ${disabled ? 'opacity-50' : ''}`}
    >
      <span className="h-3.5 w-3.5 rounded-full bg-white shadow-sm" />
    </button>
  )
}
