/**
 * LegalFooter.tsx
 * Reusable legal links bar — appears on all public pages.
 * Required by Meta for WhatsApp Business Platform app review.
 */
interface LegalFooterProps {
  /** "dark" for pages with dark backgrounds (landing), "light" for white/slate pages */
  variant?: 'dark' | 'light'
}

export default function LegalFooter({ variant = 'light' }: LegalFooterProps) {
  const linkClass =
    variant === 'dark'
      ? 'text-slate-500 hover:text-amber-400 transition-colors'
      : 'text-slate-400 hover:text-violet-600 transition-colors'

  const sepClass = variant === 'dark' ? 'text-slate-700' : 'text-slate-300'

  return (
    <div className="w-full flex flex-wrap justify-center items-center gap-x-3 gap-y-1 text-xs py-3">
      <a href="/privacy"       className={linkClass}>Privacy Policy</a>
      <span className={sepClass}>|</span>
      <a href="/data-deletion" className={linkClass}>Data Deletion</a>
      <span className={sepClass}>|</span>
      <a href="/terms"         className={linkClass}>Terms of Service</a>
      <span className={sepClass}>|</span>
      <a href="/contact"       className={linkClass}>Contact</a>
    </div>
  )
}
