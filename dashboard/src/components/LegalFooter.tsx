/**
 * LegalFooter.tsx
 * Reusable legal links bar — appears on all public pages.
 * Required by Meta for WhatsApp Business Platform app review.
 */
import { COMPANY_INFO } from '../config/companyInfo'

interface LegalFooterProps {
  /** "dark" for pages with dark backgrounds (landing), "light" for white/slate pages */
  variant?: 'dark' | 'light'
}

export default function LegalFooter({ variant = 'light' }: LegalFooterProps) {
  const linkClass =
    variant === 'dark'
      ? 'text-slate-500 hover:text-amber-400 transition-colors'
      : 'text-slate-400 hover:text-violet-600 transition-colors'

  const contactLinkClass =
    variant === 'dark'
      ? 'text-slate-400 hover:text-amber-400 transition-colors underline-offset-2 hover:underline'
      : 'text-slate-500 hover:text-violet-600 transition-colors underline-offset-2 hover:underline'

  const sepClass = variant === 'dark' ? 'text-slate-700' : 'text-slate-300'

  const entityTextClass = variant === 'dark' ? 'text-slate-400' : 'text-slate-600'
  const entityHeadingClass = variant === 'dark' ? 'text-slate-500' : 'text-slate-400'

  return (
    <div className="w-full px-4 py-3 space-y-4">
      <div className="max-w-3xl mx-auto text-center sm:text-start">
        <p className={`text-[11px] font-semibold uppercase tracking-wide mb-3 ${entityHeadingClass}`}>
          Legal Entity · الكيان القانوني
        </p>
        <div className="grid gap-4 sm:grid-cols-2 sm:gap-6">
          <div className={`text-xs leading-relaxed space-y-1 ${entityTextClass}`} dir="ltr">
            <p>{COMPANY_INFO.legalStatement.en}</p>
            <p>National Number: {COMPANY_INFO.nationalUnifiedNumber}</p>
            <p>
              National Address:
              <br />
              {COMPANY_INFO.address.enLines.map((line) => (
                <span key={line}>
                  {line}
                  <br />
                </span>
              ))}
            </p>
          </div>
          <div className={`text-xs leading-relaxed space-y-1 ${entityTextClass}`} dir="rtl">
            <p>{COMPANY_INFO.legalStatement.ar}</p>
            <p>الرقم الوطني الموحد: {COMPANY_INFO.nationalUnifiedNumber}</p>
            <p>العنوان الوطني: {COMPANY_INFO.address.ar}</p>
          </div>
        </div>

        <div
          className={`mt-4 flex flex-wrap justify-center sm:justify-start items-center gap-x-3 gap-y-1 text-xs ${entityTextClass}`}
        >
          <a
            href={COMPANY_INFO.website.url}
            className={contactLinkClass}
            target="_blank"
            rel="noopener noreferrer"
          >
            {COMPANY_INFO.website.display}
          </a>
          <span className={sepClass} aria-hidden="true">
            ·
          </span>
          <a href={`mailto:${COMPANY_INFO.email}`} className={contactLinkClass}>
            {COMPANY_INFO.email}
          </a>
          <span className={sepClass} aria-hidden="true">
            ·
          </span>
          <a href={COMPANY_INFO.phone.href} className={contactLinkClass}>
            {COMPANY_INFO.phone.display}
          </a>
        </div>
      </div>

      <div className="flex flex-wrap justify-center items-center gap-x-3 gap-y-1 text-xs">
        <a href="/privacy" className={linkClass}>
          Privacy Policy
        </a>
        <span className={sepClass}>|</span>
        <a href="/data-deletion" className={linkClass}>
          Data Deletion
        </a>
        <span className={sepClass}>|</span>
        <a href="/terms" className={linkClass}>
          Terms of Service
        </a>
        <span className={sepClass}>|</span>
        <a href="/contact" className={linkClass}>
          Contact
        </a>
      </div>
    </div>
  )
}
