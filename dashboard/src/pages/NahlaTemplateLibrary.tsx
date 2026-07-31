import { useCallback, useEffect, useMemo, useState } from 'react'
import { Link, useLocation } from 'react-router-dom'
import { BookOpen, ExternalLink, Package, RefreshCw, Store } from 'lucide-react'
import PageHeader from '../components/ui/PageHeader'
import { useLanguage } from '../i18n/context'
import {
  templatesApi,
  type NahlaLibraryTemplate,
} from '../api/templates'
import {
  ORDER_UPDATES_LIBRARY_TAG,
  filterOrderUpdatesLibraryTemplates,
  isOrderUpdatesLibraryTemplate,
} from './templates/orderUpdatesLibraryFilter'

type EcommerceFilter = 'all' | 'marketing' | typeof ORDER_UPDATES_LIBRARY_TAG

function isMarketingLibraryTemplate(tpl: NahlaLibraryTemplate): boolean {
  if (isOrderUpdatesLibraryTemplate(tpl)) return false
  if (tpl.filter_tags?.includes('marketing')) return true
  return tpl.category === 'MARKETING'
}

/**
 * E-commerce store templates surface.
 *
 * Template copy / preview / organize live here.
 * Order-update send toggles, timing, and channel ops stay in
 * /settings?tab=order_updates — never moved into this library shell.
 *
 * Open-window / Meta template policy is a documented product contract only;
 * Lifecycle routing and send orchestration are out of scope.
 */
export default function NahlaTemplateLibrary() {
  const { t, dir } = useLanguage()
  const { hash } = useLocation()
  const page = t(tr => tr.pages.ecommerceTemplates)

  const [filter, setFilter] = useState<EcommerceFilter>('all')
  const [loading, setLoading] = useState(true)
  const [templates, setTemplates] = useState<NahlaLibraryTemplate[]>([])

  const filters = useMemo(
    () =>
      [
        { key: 'all' as const, label: page.filters.all },
        { key: 'marketing' as const, label: page.filters.marketing },
        { key: ORDER_UPDATES_LIBRARY_TAG, label: page.filters.orderUpdates },
      ] satisfies { key: EcommerceFilter; label: string }[],
    [page.filters],
  )

  const load = useCallback(async (active: EcommerceFilter) => {
    setLoading(true)
    try {
      const res = await templatesApi.nahlaLibrary({})
      const all = res.templates ?? []
      let next: NahlaLibraryTemplate[]
      if (active === 'marketing') {
        next = all.filter(isMarketingLibraryTemplate)
      } else if (active === ORDER_UPDATES_LIBRARY_TAG) {
        next = filterOrderUpdatesLibraryTemplates(all)
      } else {
        next = all.filter(
          tpl => isMarketingLibraryTemplate(tpl) || isOrderUpdatesLibraryTemplate(tpl),
        )
      }
      setTemplates(next)
    } catch {
      setTemplates([])
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void load(filter)
  }, [filter, load])

  useEffect(() => {
    if (hash === '#ecommerce' || hash === '#order-updates') {
      setFilter(ORDER_UPDATES_LIBRARY_TAG)
      requestAnimationFrame(() => {
        document.getElementById('order-updates')?.scrollIntoView({
          behavior: 'smooth',
          block: 'start',
        })
      })
    }
  }, [hash])

  const emptyLabel =
    filter === ORDER_UPDATES_LIBRARY_TAG
      ? page.empty.orderUpdates
      : filter === 'marketing'
        ? page.empty.marketing
        : page.empty.all

  const showOrderUpdatesPanel =
    filter === ORDER_UPDATES_LIBRARY_TAG || filter === 'all'

  return (
    <div className="space-y-6" dir={dir}>
      <PageHeader title={page.title} subtitle={page.subtitle} />

      <div className="flex flex-wrap gap-2">
        {filters.map(item => (
          <button
            key={item.key}
            type="button"
            onClick={() => setFilter(item.key)}
            className={`px-3 py-1.5 rounded-lg text-xs font-medium transition-colors ${
              filter === item.key
                ? 'bg-brand-500 text-white'
                : 'bg-slate-100 text-slate-600 hover:bg-slate-200'
            }`}
          >
            {item.label}
          </button>
        ))}
      </div>

      {showOrderUpdatesPanel && (
        <section id="order-updates" className="card p-5 scroll-mt-24">
          <div className="flex items-start gap-3">
            <div className="w-10 h-10 rounded-xl bg-slate-100 flex items-center justify-center shrink-0">
              <Package className="w-5 h-5 text-slate-600" />
            </div>
            <div className="flex-1 min-w-0">
              <h2 className="text-sm font-semibold text-slate-900">{page.orderUpdates.title}</h2>
              <p className="text-xs text-slate-500 mt-1 leading-relaxed">
                {page.orderUpdates.description}
              </p>
              <p className="text-[11px] text-slate-400 mt-2">{page.orderUpdates.scopeNote}</p>
              <Link
                to="/settings?tab=order_updates"
                className="inline-flex items-center gap-1.5 mt-4 text-xs font-medium text-brand-600 hover:text-brand-700"
              >
                {page.orderUpdates.opsLink}
                <ExternalLink className="w-3.5 h-3.5" />
              </Link>
            </div>
          </div>
        </section>
      )}

      <section id="ecommerce" className="card p-5 scroll-mt-24">
        <div className="flex items-start gap-3 mb-4">
          <div className="w-10 h-10 rounded-xl bg-amber-50 border border-amber-200 flex items-center justify-center shrink-0">
            {filter === 'marketing' ? (
              <Store className="w-5 h-5 text-amber-600" />
            ) : (
              <BookOpen className="w-5 h-5 text-amber-600" />
            )}
          </div>
          <div className="min-w-0">
            <h2 className="text-sm font-semibold text-slate-900">{page.libraryTitle}</h2>
            <p className="text-xs text-slate-500 mt-1">{page.librarySubtitle}</p>
          </div>
        </div>

        {loading ? (
          <div className="flex items-center justify-center py-16">
            <RefreshCw className="w-6 h-6 text-amber-500 animate-spin" />
          </div>
        ) : templates.length === 0 ? (
          <p className="text-center text-slate-400 text-sm py-12">{emptyLabel}</p>
        ) : (
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
            {templates.map(tpl => {
              const isOrderUpdate = isOrderUpdatesLibraryTemplate(tpl)
              return (
                <div
                  key={tpl.key}
                  className="border border-slate-200 rounded-xl p-4 bg-white"
                >
                  {tpl.service_name_ar && (
                    <p className="text-[10px] font-semibold text-slate-500 mb-1">
                      {tpl.service_icon ? `${tpl.service_icon} ` : ''}
                      {tpl.service_name_ar}
                    </p>
                  )}
                  <p className="text-sm font-semibold text-slate-900">{tpl.name_ar}</p>
                  {tpl.description_ar && (
                    <p className="text-xs text-slate-500 mt-1 leading-relaxed line-clamp-3">
                      {tpl.description_ar}
                    </p>
                  )}
                  {tpl.preview_body && (
                    <p className="text-[11px] text-slate-400 mt-2 leading-relaxed line-clamp-2 whitespace-pre-line">
                      {tpl.preview_body}
                    </p>
                  )}
                  <div className="mt-3 flex flex-wrap gap-3">
                    <Link
                      to="/templates"
                      className="inline-flex items-center gap-1 text-xs font-medium text-brand-600 hover:text-brand-700"
                    >
                      {page.openInWhatsapp}
                      <ExternalLink className="w-3.5 h-3.5" />
                    </Link>
                    {isOrderUpdate && (
                      <Link
                        to="/settings?tab=order_updates"
                        className="inline-flex items-center gap-1 text-xs font-medium text-slate-600 hover:text-slate-800"
                      >
                        {page.orderUpdates.opsLinkShort}
                      </Link>
                    )}
                  </div>
                </div>
              )
            })}
          </div>
        )}
      </section>
    </div>
  )
}
