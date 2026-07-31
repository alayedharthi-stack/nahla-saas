import { Link } from 'react-router-dom'
import { FileText, Store, Package, Truck, ExternalLink } from 'lucide-react'
import PageHeader from '../components/ui/PageHeader'
import { useLanguage } from '../i18n/context'
import { SERVICE_ICON } from '../i18n/templatesPageLabels'

/**
 * Organizational shell for Nahla template surfaces (P3.2).
 *
 * Open-window / Meta template policy is a documented product contract only;
 * Lifecycle routing, Session vs Meta selection, and send orchestration are
 * out of scope here — this page links to existing surfaces or shows placeholders.
 */
const ORDER_UPDATE_TEMPLATE_KEYS = ['order_confirmation', 'shipping_tracking'] as const

export default function NahlaTemplateLibrary() {
  const { t } = useLanguage()
  const page = t(tr => tr.pages.nahlaTemplateLibrary)
  const ecommerce = page.sections.ecommerce
  const whatsapp = page.sections.whatsapp
  const orderUpdates = page.sections.orderUpdates

  return (
    <div className="space-y-6">
      <PageHeader title={page.title} subtitle={page.subtitle} />

      {/* 1 — E-commerce store templates (organizational placeholder) */}
      <section className="card p-5">
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

      {/* 2 — WhatsApp templates → existing /templates surface */}
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

      {/* 3 — Order update templates (informational shell — two templates only) */}
      <section className="card overflow-hidden">
        <div className="px-5 py-4 border-b border-slate-100 bg-slate-50">
          <h2 className="text-sm font-semibold text-slate-900">{orderUpdates.title}</h2>
          <p className="text-xs text-slate-500 mt-0.5">{orderUpdates.description}</p>
        </div>
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-px bg-slate-100">
          {ORDER_UPDATE_TEMPLATE_KEYS.map(key => {
            const template = orderUpdates.templates[key]
            const Icon = key === 'order_confirmation' ? Package : Truck
            return (
              <div key={key} className="bg-white p-5">
                <div className="flex items-start gap-3">
                  <div className="w-10 h-10 rounded-xl bg-slate-100 flex items-center justify-center shrink-0 text-lg">
                    {SERVICE_ICON[key] ?? <Icon className="w-5 h-5 text-slate-600" />}
                  </div>
                  <div className="flex-1 min-w-0">
                    <h3 className="text-sm font-semibold text-slate-900">{template.title}</h3>
                    <p className="text-xs text-slate-500 mt-1 leading-relaxed">{template.description}</p>
                  </div>
                </div>
              </div>
            )
          })}
        </div>
        <div className="px-5 py-3 border-t border-slate-100 bg-white">
          <Link
            to="/templates"
            className="inline-flex items-center gap-1.5 text-xs font-medium text-brand-600 hover:text-brand-700"
          >
            {orderUpdates.editLink}
            <ExternalLink className="w-3.5 h-3.5" />
          </Link>
        </div>
      </section>
    </div>
  )
}
