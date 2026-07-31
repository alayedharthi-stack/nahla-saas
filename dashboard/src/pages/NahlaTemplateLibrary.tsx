import { useEffect } from 'react'
import { Link, useLocation } from 'react-router-dom'
import { FileText, Store, ExternalLink } from 'lucide-react'
import PageHeader from '../components/ui/PageHeader'
import { useLanguage } from '../i18n/context'

/**
 * Organizational shell for Nahla template surfaces (P3.2 + daily-use nav).
 *
 * Open-window / Meta template policy is a documented product contract only;
 * Lifecycle routing, Session vs Meta selection, and send orchestration are
 * out of scope here — this page links to existing surfaces or shows placeholders.
 *
 * Order-update configuration lives under /settings?tab=order_updates — not as
 * a primary template family on this page.
 */
export default function NahlaTemplateLibrary() {
  const { t } = useLanguage()
  const { hash } = useLocation()
  const page = t(tr => tr.pages.nahlaTemplateLibrary)
  const ecommerce = page.sections.ecommerce
  const whatsapp = page.sections.whatsapp

  useEffect(() => {
    if (hash !== '#ecommerce') return
    const el = document.getElementById('ecommerce')
    if (el) el.scrollIntoView({ behavior: 'smooth', block: 'start' })
  }, [hash])

  return (
    <div className="space-y-6">
      <PageHeader title={page.title} subtitle={page.subtitle} />

      {/* E-commerce store templates — stable #ecommerce anchor for Templates Hub */}
      <section id="ecommerce" className="card p-5 scroll-mt-24">
        <div className="flex items-start gap-3">
          <div className="w-10 h-10 rounded-xl bg-slate-100 flex items-center justify-center shrink-0">
            <Store className="w-5 h-5 text-slate-600" />
          </div>
          <div className="flex-1 min-w-0">
            <h2 className="text-sm font-semibold text-slate-900">{ecommerce.title}</h2>
            <p className="text-xs text-slate-500 mt-1 leading-relaxed">{ecommerce.description}</p>
            <p className="text-xs text-slate-400 mt-3 bg-slate-50 border border-slate-100 rounded-lg px-3 py-2">
              {ecommerce.comingSoon}
            </p>
          </div>
        </div>
      </section>

      {/* WhatsApp templates → existing /templates surface */}
      <section className="card p-5">
        <div className="flex items-start gap-3">
          <div className="w-10 h-10 rounded-xl bg-slate-100 flex items-center justify-center shrink-0">
            <FileText className="w-5 h-5 text-slate-600" />
          </div>
          <div className="flex-1 min-w-0">
            <h2 className="text-sm font-semibold text-slate-900">{whatsapp.title}</h2>
            <p className="text-xs text-slate-500 mt-1 leading-relaxed">{whatsapp.description}</p>
            <Link
              to="/templates"
              className="inline-flex items-center gap-1.5 mt-4 text-xs font-medium text-brand-600 hover:text-brand-700"
            >
              {whatsapp.linkLabel}
              <ExternalLink className="w-3.5 h-3.5" />
            </Link>
          </div>
        </div>
      </section>
    </div>
  )
}
