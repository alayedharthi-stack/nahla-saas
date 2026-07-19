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

  const cardClass =
    variant === 'dark'
      ? 'border border-slate-700/70 bg-slate-800/50'
      : 'border border-slate-200 bg-slate-50'

  const cardFooterClass =
    variant === 'dark' ? 'border-t border-slate-700/70' : 'border-t border-slate-200'

  const tableDividerClass =
    variant === 'dark' ? 'border-slate-700/70' : 'border-slate-200'

  return (
    <div className="w-full px-4 py-3 space-y-4">
      <div className="max-w-3xl mx-auto">
        <section
          aria-label="Legal Entity · الكيان القانوني"
          className={`rounded-lg overflow-hidden ${cardClass}`}
        >
          <table
            className={`w-full table-fixed border-collapse text-xs leading-relaxed ${entityTextClass}`}
            dir="ltr"
          >
            <colgroup>
              <col className="w-1/2" />
              <col className="w-1/2" />
            </colgroup>
            <thead>
              <tr>
                <th
                  scope="col"
                  className={`break-words px-3 py-2.5 text-left align-top font-semibold uppercase tracking-wide sm:px-4 ${entityHeadingClass}`}
                  dir="ltr"
                >
                  Legal Entity
                </th>
                <th
                  scope="col"
                  className={`border-l break-words px-3 py-2.5 text-right align-top font-semibold uppercase tracking-wide sm:px-4 ${entityHeadingClass} ${tableDividerClass}`}
                  dir="rtl"
                >
                  الكيان القانوني
                </th>
              </tr>
            </thead>
            <tbody>
              <tr className={`border-t ${tableDividerClass}`}>
                <td className="break-words px-3 py-2.5 align-top sm:px-4" dir="ltr">
                  {COMPANY_INFO.legalStatement.en}
                </td>
                <td
                  className={`border-l break-words px-3 py-2.5 text-right align-top sm:px-4 ${tableDividerClass}`}
                  dir="rtl"
                >
                  {COMPANY_INFO.legalStatement.ar}
                </td>
              </tr>
              <tr className={`border-t ${tableDividerClass}`}>
                <td className="break-words px-3 py-2 font-medium align-top sm:px-4" dir="ltr">
                  National Number:
                </td>
                <td
                  className={`border-l break-words px-3 py-2 text-right font-medium align-top sm:px-4 ${tableDividerClass}`}
                  dir="rtl"
                >
                  الرقم الوطني الموحد:
                </td>
              </tr>
              <tr className={`border-t ${tableDividerClass}`}>
                <td className="break-words px-3 py-2.5 align-top sm:px-4" dir="ltr">
                  {COMPANY_INFO.nationalUnifiedNumber}
                </td>
                <td
                  className={`border-l break-words px-3 py-2.5 text-right align-top sm:px-4 ${tableDividerClass}`}
                  dir="rtl"
                >
                  {COMPANY_INFO.nationalUnifiedNumber}
                </td>
              </tr>
              <tr className={`border-t ${tableDividerClass}`}>
                <td className="break-words px-3 py-2 font-medium align-top sm:px-4" dir="ltr">
                  National Address:
                </td>
                <td
                  className={`border-l break-words px-3 py-2 text-right font-medium align-top sm:px-4 ${tableDividerClass}`}
                  dir="rtl"
                >
                  العنوان الوطني:
                </td>
              </tr>
              <tr className={`border-t ${tableDividerClass}`}>
                <td className="break-words px-3 py-2.5 align-top sm:px-4" dir="ltr">
                  {COMPANY_INFO.address.enLines.map((line) => (
                    <span key={line}>
                      {line}
                      <br />
                    </span>
                  ))}
                </td>
                <td
                  className={`border-l break-words px-3 py-2.5 text-right align-top sm:px-4 ${tableDividerClass}`}
                  dir="rtl"
                >
                  {COMPANY_INFO.address.ar}
                </td>
              </tr>
            </tbody>
          </table>

          <footer className={`px-4 py-3 ${cardFooterClass}`}>
            <div
              className={`flex flex-wrap justify-center sm:justify-start items-center gap-x-3 gap-y-2 text-xs ${entityTextClass}`}
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
          </footer>
        </section>
      </div>

      <div className="flex flex-wrap justify-center items-center gap-x-3 gap-y-1 text-xs">
        <a href="/privacy" className={linkClass}>
          Privacy Policy
        </a>
        <span className={sepClass} aria-hidden="true">
          |
        </span>
        <a href="/data-deletion" className={linkClass}>
          Data Deletion
        </a>
        <span className={sepClass} aria-hidden="true">
          |
        </span>
        <a href="/terms" className={linkClass}>
          Terms of Service
        </a>
        <span className={sepClass} aria-hidden="true">
          |
        </span>
        <a href="/contact" className={linkClass}>
          Contact
        </a>
      </div>
    </div>
  )
}
