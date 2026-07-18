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
            <p>
              Nahlah AI is a technology platform owned and operated by Nahlah Ai Establishment,
              Saudi Arabia.
            </p>
            <p>National Number: 7050202485</p>
            <p>
              National Address:
              <br />
              Al Halaqa Western 1,
              <br />
              Al Halqah Al Gharbia District,
              <br />
              At Taif 26563,
              <br />
              Kingdom of Saudi Arabia.
            </p>
          </div>
          <div className={`text-xs leading-relaxed space-y-1 ${entityTextClass}`} dir="rtl">
            <p>
              نحلة AI منصة تقنية مملوكة ومشغلة من مؤسسة نحلة أي آي، المملكة العربية السعودية.
            </p>
            <p>الرقم الوطني الموحد: 7050202485</p>
            <p>
              العنوان الوطني: الحلقة الغربية 1، حي الحلقة الغربية، الطائف 26563، المملكة العربية
              السعودية.
            </p>
          </div>
        </div>
      </div>

      <div className="flex flex-wrap justify-center items-center gap-x-3 gap-y-1 text-xs">
        <a href="/privacy"       className={linkClass}>Privacy Policy</a>
        <span className={sepClass}>|</span>
        <a href="/data-deletion" className={linkClass}>Data Deletion</a>
        <span className={sepClass}>|</span>
        <a href="/terms"         className={linkClass}>Terms of Service</a>
        <span className={sepClass}>|</span>
        <a href="/contact"       className={linkClass}>Contact</a>
      </div>
    </div>
  )
}
