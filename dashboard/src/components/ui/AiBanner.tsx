import { Sparkles, Bot } from 'lucide-react'

/**
 * AiBanner
 * ────────
 * Communicates that the page below is **AI-managed** — the merchant
 * sets the strategy (rules, limits, conditions, on/off), and Nahla
 * Autopilot decides *when*, *who*, and *how* to apply the incentive.
 *
 * Use at the top of any page where the data on display is generated
 * or orchestrated by the Autopilot (Coupons, Promotions, etc.) so
 * the merchant never confuses these screens with a manual catalog.
 */

interface AiBannerProps {
  title:    string
  body:     string
  /** A short bullet list of "what the AI handles for you". */
  bullets?: string[]
  /** Override the icon (defaults to Bot). */
  icon?:    'bot' | 'sparkles'
  /** Compact one-line variant for inside cards/sections. */
  compact?: boolean
}

export default function AiBanner({
  title,
  body,
  bullets,
  icon    = 'bot',
  compact = false,
}: AiBannerProps) {
  const Icon = icon === 'sparkles' ? Sparkles : Bot

  if (compact) {
    return (
      <div className="flex items-start gap-2 rounded-lg border border-amber-200/70 bg-amber-50/70 px-3 py-2">
        <span className="relative shrink-0 mt-0.5">
          <Icon className="w-4 h-4 text-amber-600" />
          <span className="absolute -bottom-1.5 -end-1.5 inline-flex items-center px-1 py-px rounded bg-amber-500/15 border border-amber-500/50">
            <span className="text-[6px] font-black text-amber-600 leading-none">AI</span>
          </span>
        </span>
        <div className="min-w-0">
          <p className="text-xs font-semibold text-amber-900">{title}</p>
          <p className="text-[11px] text-amber-800/80 leading-relaxed mt-0.5">{body}</p>
        </div>
      </div>
    )
  }

  return (
    <div className="relative overflow-hidden rounded-2xl border border-amber-200 bg-gradient-to-br from-amber-50 via-orange-50/60 to-white px-5 py-4">
      <div className="absolute -top-8 -end-8 w-32 h-32 bg-amber-200/30 rounded-full blur-3xl" aria-hidden />
      <div className="relative flex items-start gap-4">
        <div className="shrink-0 w-10 h-10 rounded-xl bg-gradient-to-br from-amber-400 to-orange-500 text-white flex items-center justify-center shadow-md shadow-amber-200">
          <Icon className="w-5 h-5" />
        </div>
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2">
            <h3 className="text-sm font-bold text-amber-900">{title}</h3>
            <span className="inline-flex items-center px-1.5 py-0.5 rounded bg-amber-500/15 border border-amber-500/50">
              <span className="text-[9px] font-black text-amber-700 leading-none tracking-wider">AI</span>
            </span>
          </div>
          <p className="text-xs text-amber-900/80 leading-relaxed mt-1">{body}</p>
          {bullets && bullets.length > 0 && (
            <ul className="grid sm:grid-cols-2 gap-x-4 gap-y-1 mt-2.5">
              {bullets.map((b, i) => (
                <li key={i} className="flex items-center gap-1.5 text-[11px] text-amber-900/75">
                  <span className="w-1 h-1 rounded-full bg-amber-500 shrink-0" />
                  <span>{b}</span>
                </li>
              ))}
            </ul>
          )}
        </div>
      </div>
    </div>
  )
}
